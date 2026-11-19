import csv
import hmac
import io
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)


BASE_DIR = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("EXPENSE_DATABASE", BASE_DIR / "expenses.db"))
CATEGORIES = ("Food", "Travel", "Rent", "Shopping", "Bills", "Other")
INCOME_SOURCES = ("Salary", "Freelance", "Business", "Investment", "Gift", "Other")
MAX_NOTE_LENGTH = 200

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "local-development-key-change-me")


@contextmanager
def get_db_connection():
    """Open a SQLite connection and make query results easy to read by column name."""
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database():
    """Create the tables once, and keep the budget setting available."""
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                category TEXT NOT NULL,
                expense_date TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                monthly_budget_cents INTEGER NOT NULL DEFAULT 0
                    CHECK (monthly_budget_cents >= 0)
            )
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings (id, monthly_budget_cents) VALUES (1, 0)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
            source TEXT NOT NULL,
            income_date TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT ''
            )
            """
        )


def parse_money(value):
    """Return a positive amount as integer cents, or raise ValueError."""
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("Enter a valid amount, for example 12.50.") from None

    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise ValueError("Amount must be greater than zero and have at most 2 decimal places.")
    if amount * 100 > 9_223_372_036_854_775_807:
        raise ValueError("Amount is too large to save.")

    return int(amount * 100)


def format_money(cents):
    """Display integer cents as a familiar two-decimal amount."""
    return f"{Decimal(cents) / 100:,.2f}"


def month_keys_ending_with_current(count=6):
    """Return the last several calendar months in oldest-to-newest order."""
    today = date.today()
    year, month = today.year, today.month
    months = []
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    return list(reversed(months))


def valid_month(value):
    """Return whether a YYYY-MM month value represents a real calendar month."""
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        return False
    try:
        date.fromisoformat(f"{value}-01")
    except ValueError:
        return False
    return True


def csrf_token():
    """Create a session token used to reject cross-site form submissions."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(16)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf_token():
    if request.method == "POST":
        supplied = request.form.get("csrf_token", "")
        expected = session.get("_csrf_token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            abort(400, description="The form expired. Reload the page and try again.")


@app.template_filter("money")
def money_filter(cents):
    return format_money(cents)


@app.route("/")
def index():
    current_month = date.today().strftime("%Y-%m")
    months_for_chart = month_keys_ending_with_current()
    with get_db_connection() as connection:
        expense_total = connection.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0)
            FROM expenses
            WHERE substr(expense_date, 1, 7) = ?
            """,
            (current_month,),
        ).fetchone()[0]
        income_total = connection.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0)
            FROM incomes
            WHERE substr(income_date, 1, 7) = ?
            """,
            (current_month,),
        ).fetchone()[0]
        expense_count = connection.execute(
            "SELECT COUNT(*) FROM expenses WHERE substr(expense_date, 1, 7) = ?",
            (current_month,),
        ).fetchone()[0]
        income_count = connection.execute(
            "SELECT COUNT(*) FROM incomes WHERE substr(income_date, 1, 7) = ?",
            (current_month,),
        ).fetchone()[0]
        highest_category_row = connection.execute(
            """
            SELECT category, SUM(amount_cents) AS total_cents
            FROM expenses
            WHERE substr(expense_date, 1, 7) = ?
            GROUP BY category
            ORDER BY total_cents DESC, category ASC
            LIMIT 1
            """,
            (current_month,),
        ).fetchone()
        budget_cents = connection.execute(
            "SELECT monthly_budget_cents FROM settings WHERE id = 1"
        ).fetchone()[0]
        category_totals = connection.execute(
            """
            SELECT category, SUM(amount_cents) AS total_cents
            FROM expenses
            WHERE substr(expense_date, 1, 7) = ?
            GROUP BY category
            ORDER BY category
            """,
            (current_month,),
        ).fetchall()
        monthly_totals = connection.execute(
            """
            SELECT month, SUM(expense_cents) AS expense_cents, SUM(income_cents) AS income_cents
            FROM (
                SELECT substr(expense_date, 1, 7) AS month,
                       SUM(amount_cents) AS expense_cents, 0 AS income_cents
                FROM expenses
                WHERE substr(expense_date, 1, 7) BETWEEN ? AND ?
                GROUP BY month
                UNION ALL
                SELECT substr(income_date, 1, 7) AS month,
                       0 AS expense_cents, SUM(amount_cents) AS income_cents
                FROM incomes
                WHERE substr(income_date, 1, 7) BETWEEN ? AND ?
                GROUP BY month
            )
            GROUP BY month
            """,
            (
                months_for_chart[0],
                months_for_chart[-1],
                months_for_chart[0],
                months_for_chart[-1],
            ),
        ).fetchall()
        recent_expenses = connection.execute(
            """
            SELECT id, amount_cents, category, expense_date, note
            FROM expenses
            ORDER BY expense_date DESC, id DESC
            LIMIT 5
            """
        ).fetchall()

    monthly_totals_by_month = {row["month"]: row for row in monthly_totals}
    highest_category = highest_category_row["category"] if highest_category_row else "—"
    budget_percent = (expense_total / budget_cents * 100) if budget_cents else 0
    if budget_cents and expense_total >= budget_cents:
        budget_warning = "over"
    elif budget_cents and expense_total * 100 >= budget_cents * 80:
        budget_warning = "near"
    else:
        budget_warning = ""

    return render_template(
        "index.html",
        current_month=current_month,
        expense_total=expense_total,
        income_total=income_total,
        net_total=income_total - expense_total,
        expense_count=expense_count,
        income_count=income_count,
        highest_category=highest_category,
        recent_expenses=recent_expenses,
        budget_cents=budget_cents,
        budget_amount=format_money(budget_cents),
        budget_percent=budget_percent,
        budget_warning=budget_warning,
        category_labels=[row["category"] for row in category_totals],
        category_values=[row["total_cents"] / 100 for row in category_totals],
        month_labels=months_for_chart,
        month_expenses=[
            monthly_totals_by_month.get(month, {"expense_cents": 0})["expense_cents"] / 100
            for month in months_for_chart
        ],
        month_incomes=[
            monthly_totals_by_month.get(month, {"income_cents": 0})["income_cents"] / 100
            for month in months_for_chart
        ],
    )


@app.get("/expenses")
def expenses_page():
    selected_month = request.args.get("month", "").strip()
    selected_category = request.args.get("category", "").strip()
    if selected_month and not valid_month(selected_month):
        flash("Choose a valid month to filter expenses.", "warning")
        selected_month = ""
    if selected_category and selected_category not in CATEGORIES:
        flash("Choose a category from the list.", "warning")
        selected_category = ""

    filters = []
    parameters = []
    if selected_month:
        filters.append("substr(expense_date, 1, 7) = ?")
        parameters.append(selected_month)
    if selected_category:
        filters.append("category = ?")
        parameters.append(selected_category)
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    with get_db_connection() as connection:
        expenses = connection.execute(
            f"""
            SELECT id, amount_cents, category, expense_date, note
            FROM expenses
            {where_clause}
            ORDER BY expense_date DESC, id DESC
            """,
            parameters,
        ).fetchall()

    return render_template(
        "expenses.html",
        expenses=expenses,
        categories=CATEGORIES,
        selected_month=selected_month,
        selected_category=selected_category,
    )


@app.get("/income")
def income_page():
    selected_month = request.args.get("month", "").strip()
    selected_source = request.args.get("source", "").strip()
    if selected_month and not valid_month(selected_month):
        flash("Choose a valid month to filter income.", "warning")
        selected_month = ""
    if selected_source and selected_source not in INCOME_SOURCES:
        flash("Choose an income source from the list.", "warning")
        selected_source = ""

    filters = []
    parameters = []
    if selected_month:
        filters.append("substr(income_date, 1, 7) = ?")
        parameters.append(selected_month)
    if selected_source:
        filters.append("source = ?")
        parameters.append(selected_source)
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    with get_db_connection() as connection:
        incomes = connection.execute(
            f"""
            SELECT id, amount_cents, source, income_date, note
            FROM incomes
            {where_clause}
            ORDER BY income_date DESC, id DESC
            """,
            parameters,
        ).fetchall()

    return render_template(
        "income.html",
        incomes=incomes,
        sources=INCOME_SOURCES,
        selected_month=selected_month,
        selected_source=selected_source,
    )


@app.get("/reports")
def reports_page():
    selected_month = request.args.get("month", date.today().strftime("%Y-%m")).strip()
    if not valid_month(selected_month):
        flash("Choose a valid month for the report.", "warning")
        selected_month = date.today().strftime("%Y-%m")

    month_keys = month_keys_ending_with_current()
    with get_db_connection() as connection:
        expense_total = connection.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) FROM expenses
            WHERE substr(expense_date, 1, 7) = ?
            """,
            (selected_month,),
        ).fetchone()[0]
        income_total = connection.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) FROM incomes
            WHERE substr(income_date, 1, 7) = ?
            """,
            (selected_month,),
        ).fetchone()[0]
        category_totals = connection.execute(
            """
            SELECT category, SUM(amount_cents) AS total_cents
            FROM expenses
            WHERE substr(expense_date, 1, 7) = ?
            GROUP BY category
            ORDER BY total_cents DESC
            """,
            (selected_month,),
        ).fetchall()
        source_totals = connection.execute(
            """
            SELECT source, SUM(amount_cents) AS total_cents
            FROM incomes
            WHERE substr(income_date, 1, 7) = ?
            GROUP BY source
            ORDER BY total_cents DESC
            """,
            (selected_month,),
        ).fetchall()
        trend_rows = connection.execute(
            """
            SELECT month, SUM(expense_cents) AS expense_cents, SUM(income_cents) AS income_cents
            FROM (
                SELECT substr(expense_date, 1, 7) AS month,
                       SUM(amount_cents) AS expense_cents, 0 AS income_cents
                FROM expenses
                WHERE substr(expense_date, 1, 7) BETWEEN ? AND ?
                GROUP BY month
                UNION ALL
                SELECT substr(income_date, 1, 7) AS month,
                       0 AS expense_cents, SUM(amount_cents) AS income_cents
                FROM incomes
                WHERE substr(income_date, 1, 7) BETWEEN ? AND ?
                GROUP BY month
            )
            GROUP BY month
            """,
            (month_keys[0], month_keys[-1], month_keys[0], month_keys[-1]),
        ).fetchall()

    trend_by_month = {row["month"]: row for row in trend_rows}
    return render_template(
        "reports.html",
        selected_month=selected_month,
        expense_total=expense_total,
        income_total=income_total,
        net_total=income_total - expense_total,
        category_totals=category_totals,
        source_totals=source_totals,
        month_labels=month_keys,
        month_expenses=[
            trend_by_month.get(month, {"expense_cents": 0})["expense_cents"] / 100
            for month in month_keys
        ],
        month_incomes=[
            trend_by_month.get(month, {"income_cents": 0})["income_cents"] / 100
            for month in month_keys
        ],
    )


@app.route("/expenses/add", methods=["GET", "POST"])
def add_expense():
    values = {"amount": "", "category": "", "expense_date": date.today().isoformat(), "note": ""}
    if request.method == "POST":
        values = {
            "amount": request.form.get("amount", "").strip(),
            "category": request.form.get("category", "").strip(),
            "expense_date": request.form.get("expense_date", "").strip(),
            "note": request.form.get("note", "").strip(),
        }
        try:
            amount_cents = parse_money(values["amount"])
            expense_date = date.fromisoformat(values["expense_date"])
            if values["category"] not in CATEGORIES:
                raise ValueError("Choose a category from the list.")
            if len(values["note"]) > MAX_NOTE_LENGTH:
                raise ValueError(f"Keep the note to {MAX_NOTE_LENGTH} characters or fewer.")
        except ValueError as error:
            flash(str(error), "danger")
            return render_template("expense_form.html", mode="Add", values=values, categories=CATEGORIES)

        with get_db_connection() as connection:
            connection.execute(
                """
                INSERT INTO expenses (amount_cents, category, expense_date, note)
                VALUES (?, ?, ?, ?)
                """,
                (amount_cents, values["category"], expense_date.isoformat(), values["note"]),
            )
        flash("Expense added.", "success")
        return redirect(url_for("index"))

    return render_template("expense_form.html", mode="Add", values=values, categories=CATEGORIES)


@app.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
def edit_expense(expense_id):
    with get_db_connection() as connection:
        expense = connection.execute(
            "SELECT * FROM expenses WHERE id = ?", (expense_id,)
        ).fetchone()
    if expense is None:
        abort(404)

    values = {
        "amount": format_money(expense["amount_cents"]),
        "category": expense["category"],
        "expense_date": expense["expense_date"],
        "note": expense["note"],
    }
    if request.method == "POST":
        values = {
            "amount": request.form.get("amount", "").strip(),
            "category": request.form.get("category", "").strip(),
            "expense_date": request.form.get("expense_date", "").strip(),
            "note": request.form.get("note", "").strip(),
        }
        try:
            amount_cents = parse_money(values["amount"])
            expense_date = date.fromisoformat(values["expense_date"])
            if values["category"] not in CATEGORIES:
                raise ValueError("Choose a category from the list.")
            if len(values["note"]) > MAX_NOTE_LENGTH:
                raise ValueError(f"Keep the note to {MAX_NOTE_LENGTH} characters or fewer.")
        except ValueError as error:
            flash(str(error), "danger")
            return render_template(
                "expense_form.html", mode="Edit", values=values, categories=CATEGORIES
            )

        with get_db_connection() as connection:
            connection.execute(
                """
                UPDATE expenses
                SET amount_cents = ?, category = ?, expense_date = ?, note = ?
                WHERE id = ?
                """,
                (amount_cents, values["category"], expense_date.isoformat(), values["note"], expense_id),
            )
        flash("Expense updated.", "success")
        return redirect(url_for("index"))

    return render_template("expense_form.html", mode="Edit", values=values, categories=CATEGORIES)


@app.post("/expenses/<int:expense_id>/delete")
def delete_expense(expense_id):
    with get_db_connection() as connection:
        cursor = connection.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    if cursor.rowcount == 0:
        abort(404)
    flash("Expense deleted.", "success")
    return redirect(url_for("index"))


@app.route("/income/add", methods=["GET", "POST"])
def add_income():
    values = {
        "amount": "",
        "source": "",
        "transaction_date": date.today().isoformat(),
        "note": "",
    }
    if request.method == "POST":
        values = {
            "amount": request.form.get("amount", "").strip(),
            "source": request.form.get("source", "").strip(),
            "transaction_date": request.form.get("transaction_date", "").strip(),
            "note": request.form.get("note", "").strip(),
        }
        try:
            amount_cents = parse_money(values["amount"])
            income_date = date.fromisoformat(values["transaction_date"])
            if values["source"] not in INCOME_SOURCES:
                raise ValueError("Choose an income source from the list.")
            if len(values["note"]) > MAX_NOTE_LENGTH:
                raise ValueError(f"Keep the note to {MAX_NOTE_LENGTH} characters or fewer.")
        except ValueError as error:
            flash(str(error), "danger")
            return render_template(
                "transaction_form.html",
                mode="Add",
                kind="income",
                values=values,
                choices=INCOME_SOURCES,
            )

        with get_db_connection() as connection:
            connection.execute(
                """
                INSERT INTO incomes (amount_cents, source, income_date, note)
                VALUES (?, ?, ?, ?)
                """,
                (amount_cents, values["source"], income_date.isoformat(), values["note"]),
            )
        flash("Income added.", "success")
        return redirect(url_for("income_page"))

    return render_template(
        "transaction_form.html",
        mode="Add",
        kind="income",
        values=values,
        choices=INCOME_SOURCES,
    )


@app.route("/income/<int:income_id>/edit", methods=["GET", "POST"])
def edit_income(income_id):
    with get_db_connection() as connection:
        income = connection.execute(
            "SELECT * FROM incomes WHERE id = ?", (income_id,)
        ).fetchone()
    if income is None:
        abort(404)

    values = {
        "amount": format_money(income["amount_cents"]),
        "source": income["source"],
        "transaction_date": income["income_date"],
        "note": income["note"],
    }
    if request.method == "POST":
        values = {
            "amount": request.form.get("amount", "").strip(),
            "source": request.form.get("source", "").strip(),
            "transaction_date": request.form.get("transaction_date", "").strip(),
            "note": request.form.get("note", "").strip(),
        }
        try:
            amount_cents = parse_money(values["amount"])
            income_date = date.fromisoformat(values["transaction_date"])
            if values["source"] not in INCOME_SOURCES:
                raise ValueError("Choose an income source from the list.")
            if len(values["note"]) > MAX_NOTE_LENGTH:
                raise ValueError(f"Keep the note to {MAX_NOTE_LENGTH} characters or fewer.")
        except ValueError as error:
            flash(str(error), "danger")
            return render_template(
                "transaction_form.html",
                mode="Edit",
                kind="income",
                values=values,
                choices=INCOME_SOURCES,
            )

        with get_db_connection() as connection:
            connection.execute(
                """
                UPDATE incomes
                SET amount_cents = ?, source = ?, income_date = ?, note = ?
                WHERE id = ?
                """,
                (
                    amount_cents,
                    values["source"],
                    income_date.isoformat(),
                    values["note"],
                    income_id,
                ),
            )
        flash("Income updated.", "success")
        return redirect(url_for("income_page"))

    return render_template(
        "transaction_form.html",
        mode="Edit",
        kind="income",
        values=values,
        choices=INCOME_SOURCES,
    )


@app.post("/income/<int:income_id>/delete")
def delete_income(income_id):
    with get_db_connection() as connection:
        cursor = connection.execute("DELETE FROM incomes WHERE id = ?", (income_id,))
    if cursor.rowcount == 0:
        abort(404)
    flash("Income deleted.", "success")
    return redirect(url_for("income_page"))


@app.route("/budget", methods=["GET", "POST"])
def update_budget():
    if request.method == "POST":
        try:
            budget_cents = parse_money(request.form.get("budget", ""))
        except ValueError as error:
            flash(str(error), "danger")
            return redirect(url_for("update_budget"))

        with get_db_connection() as connection:
            connection.execute(
                "UPDATE settings SET monthly_budget_cents = ? WHERE id = 1",
                (budget_cents,),
            )
        flash("Monthly budget saved.", "success")
        return redirect(url_for("update_budget"))

    current_month = date.today().strftime("%Y-%m")
    with get_db_connection() as connection:
        budget_cents = connection.execute(
            "SELECT monthly_budget_cents FROM settings WHERE id = 1"
        ).fetchone()[0]
        expense_total = connection.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) FROM expenses
            WHERE substr(expense_date, 1, 7) = ?
            """,
            (current_month,),
        ).fetchone()[0]

    budget_percent = expense_total / budget_cents * 100 if budget_cents else 0
    if budget_cents and expense_total >= budget_cents:
        budget_warning = "over"
    elif budget_cents and expense_total * 100 >= budget_cents * 80:
        budget_warning = "near"
    else:
        budget_warning = ""
    return render_template(
        "budget.html",
        current_month=current_month,
        budget_cents=budget_cents,
        budget_amount=format_money(budget_cents),
        expense_total=expense_total,
        budget_percent=budget_percent,
        budget_warning=budget_warning,
    )


@app.get("/reports.csv")
def download_report_csv():
    selected_month = request.args.get("month", date.today().strftime("%Y-%m")).strip()
    if not valid_month(selected_month):
        abort(400, description="Choose a valid month for the report.")

    with get_db_connection() as connection:
        expenses = connection.execute(
            """
            SELECT amount_cents, category, expense_date, note FROM expenses
            WHERE substr(expense_date, 1, 7) = ?
            """,
            (selected_month,),
        ).fetchall()
        incomes = connection.execute(
            """
            SELECT amount_cents, source, income_date, note FROM incomes
            WHERE substr(income_date, 1, 7) = ?
            """,
            (selected_month,),
        ).fetchall()

    rows = [
        ("Expense", row["category"], row["expense_date"], row["amount_cents"], row["note"])
        for row in expenses
    ]
    rows.extend(
        ("Income", row["source"], row["income_date"], row["amount_cents"], row["note"])
        for row in incomes
    )
    rows.sort(key=lambda row: row[2], reverse=True)

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("Type", "Category / Source", "Date", "Amount", "Note"))
    for transaction_type, label, transaction_date, amount_cents, note in rows:
        writer.writerow(
            (transaction_type, label, transaction_date, format_money(amount_cents), note)
        )

    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        f"attachment; filename=finance-report-{selected_month}.csv"
    )
    return response


@app.get("/expenses.csv")
def download_csv():
    with get_db_connection() as connection:
        expenses = connection.execute(
            """
            SELECT amount_cents, category, expense_date, note
            FROM expenses
            ORDER BY expense_date DESC, id DESC
            """
        ).fetchall()

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("Amount", "Category", "Date", "Note"))
    for expense in expenses:
        writer.writerow(
            (
                format_money(expense["amount_cents"]),
                expense["category"],
                expense["expense_date"],
                expense["note"],
            )
        )

    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=expenses.csv"
    return response


initialize_database()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
