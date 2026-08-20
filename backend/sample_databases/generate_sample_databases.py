"""Generates the sample SQLite databases in this folder.

These are ready-to-upload example databases for trying out the
"Upload your own SQLite (.db) file" feature (see db_connections.py /
POST /database/upload) without needing to build your own schema first.

Run with:
    python generate_sample_databases.py

Each database has two related tables (with a foreign key) so the
Entity-Relationship diagram view has something meaningful to show.
"""

import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))


def build_bookstore_db():
    path = os.path.join(HERE, "bookstore.db")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE authors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            country TEXT NOT NULL,
            birth_year INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author_id INTEGER NOT NULL REFERENCES authors(id),
            genre TEXT NOT NULL,
            price REAL NOT NULL,
            published_year INTEGER
        )
    """)

    cur.executemany(
        "INSERT INTO authors (name, country, birth_year) VALUES (?, ?, ?)",
        [
            ("George Orwell", "UK", 1903),
            ("Chinua Achebe", "Nigeria", 1930),
            ("Haruki Murakami", "Japan", 1949),
            ("Isabel Allende", "Chile", 1942),
            ("Yuval Noah Harari", "Israel", 1976),
        ],
    )
    cur.executemany(
        "INSERT INTO books (title, author_id, genre, price, published_year) VALUES (?, ?, ?, ?, ?)",
        [
            ("1984", 1, "Dystopian", 12.99, 1949),
            ("Animal Farm", 1, "Satire", 8.99, 1945),
            ("Things Fall Apart", 2, "Fiction", 10.50, 1958),
            ("Norwegian Wood", 3, "Fiction", 14.25, 1987),
            ("Kafka on the Shore", 3, "Fiction", 15.00, 2002),
            ("The House of the Spirits", 4, "Fiction", 13.75, 1982),
            ("Sapiens", 5, "Non-fiction", 18.99, 2011),
        ],
    )

    conn.commit()
    conn.close()
    print(f"Created {path}")


def build_employees_db():
    path = os.path.join(HERE, "employees.db")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            location TEXT NOT NULL,
            budget REAL NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            department_id INTEGER NOT NULL REFERENCES departments(id),
            position TEXT NOT NULL,
            salary REAL NOT NULL,
            hire_date TEXT NOT NULL
        )
    """)

    cur.executemany(
        "INSERT INTO departments (name, location, budget) VALUES (?, ?, ?)",
        [
            ("Engineering", "Bengaluru", 500000.00),
            ("Sales", "Mumbai", 250000.00),
            ("Marketing", "Delhi", 150000.00),
            ("Human Resources", "Chennai", 100000.00),
        ],
    )
    cur.executemany(
        "INSERT INTO employees (name, department_id, position, salary, hire_date) VALUES (?, ?, ?, ?, ?)",
        [
            ("Ananya Rao", 1, "Software Engineer", 950000.00, "2022-03-14"),
            ("Rohan Mehta", 1, "Engineering Manager", 1600000.00, "2019-07-01"),
            ("Priya Nair", 2, "Sales Executive", 700000.00, "2021-11-20"),
            ("Karan Malhotra", 2, "Sales Manager", 1200000.00, "2020-01-10"),
            ("Simran Kaur", 3, "Marketing Specialist", 800000.00, "2023-02-05"),
            ("Arjun Verma", 4, "HR Coordinator", 650000.00, "2022-09-18"),
        ],
    )

    conn.commit()
    conn.close()
    print(f"Created {path}")


def build_inventory_db():
    path = os.path.join(HERE, "inventory.db")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact_email TEXT NOT NULL,
            country TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE inventory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL
        )
    """)

    cur.executemany(
        "INSERT INTO suppliers (name, contact_email, country) VALUES (?, ?, ?)",
        [
            ("Global Parts Co.", "sales@globalparts.example", "USA"),
            ("Nordic Supplies AB", "contact@nordicsupplies.example", "Sweden"),
            ("Pacific Traders", "info@pacifictraders.example", "Singapore"),
            ("Iberia Components", "hola@iberiacomponents.example", "Spain"),
        ],
    )
    cur.executemany(
        "INSERT INTO inventory_items (name, supplier_id, quantity, unit_price) VALUES (?, ?, ?, ?)",
        [
            ("Steel Bolts (M8)", 1, 5000, 0.05),
            ("Aluminum Sheet 1m x 1m", 1, 300, 22.50),
            ("Ball Bearings", 2, 1200, 1.75),
            ("Rubber Gaskets", 2, 2000, 0.35),
            ("Circuit Boards", 3, 800, 4.20),
            ("Copper Wiring (per meter)", 4, 4000, 0.65),
        ],
    )

    conn.commit()
    conn.close()
    print(f"Created {path}")


if __name__ == "__main__":
    build_bookstore_db()
    build_employees_db()
    build_inventory_db()
