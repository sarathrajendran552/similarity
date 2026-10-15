#!/usr/bin/env python3
import argparse
import csv
import getpass
import mariadb

OUTPUT_CSV = "template_report.csv"

QUERY = """
SELECT
    emp_code,
    actual_template,
    template_hash,
    occurrences
FROM (
    SELECT
        SHA2(b.bio_tmp, 256) AS template_hash,
        ANY_VALUE(b.bio_tmp) AS actual_template,
        GROUP_CONCAT(
            DISTINCT p.emp_code
            ORDER BY p.emp_code
            SEPARATOR ';'
        ) AS emp_code,
        COUNT(*) AS occurrences
    FROM iclock_biodata AS b
    JOIN personnel_employee AS p
        ON p.id = b.employee_id
    GROUP BY SHA2(b.bio_tmp, 256)
) AS templates
ORDER BY template_hash
LIMIT 3
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Query MariaDB and export template results to CSV."
    )

    parser.add_argument("--host", required=True, help="Database hostname")
    parser.add_argument("--port", type=int, default=3306, help="Database port")
    parser.add_argument("--user", required=True, help="Database username")
    parser.add_argument("--password", help="Database password (will prompt if omitted)")
    parser.add_argument("--database", required=True, help="Database name")
    parser.add_argument(
        "--output",
        default=OUTPUT_CSV,
        help="Output CSV filename",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    password = args.password
    if not password:
        password = getpass.getpass(prompt="Database password: ")

    conn = mariadb.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=password,
        database=args.database,
    )

    try:
        cursor = conn.cursor()
        cursor.execute(QUERY)

        columns = [col[0] for col in cursor.description]

        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows(cursor)

        print(f"Exported results to {args.output}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()