#!/usr/bin/env python3

import argparse
import csv
import getpass
import sys
import mariadb

OUTPUT_CSV = "emp_codes_templates_by_punch_time.csv"

QUERY = """
SELECT
    t.emp_code,
    b.bio_tmp AS actual_template
FROM iclock_transaction AS t
JOIN personnel_employee AS p
    ON p.emp_code = t.emp_code
JOIN iclock_biodata AS b
    ON b.employee_id = p.id
WHERE t.punch_time >= ?
  AND t.punch_time <= ?
ORDER BY t.punch_time ASC, t.emp_code ASC
"""

def main():
    parser = argparse.ArgumentParser(
        description="Export employee biometric templates by punch time."
    )

    # Database connection arguments
    parser.add_argument("--host", required=True, help="Database hostname")
    parser.add_argument("--port", type=int, default=3306, help="Database port")
    parser.add_argument("--user", required=True, help="Database username")
    parser.add_argument("--password", help="Database password (will prompt if omitted)")
    parser.add_argument("--database", required=True, help="Database name")

    # Positional and output arguments
    parser.add_argument(
        "start_time",
        help="Start time (YYYY-MM-DD HH:MM:SS)"
    )
    parser.add_argument(
        "end_time",
        help="End time (YYYY-MM-DD HH:MM:SS)"
    )
    parser.add_argument(
        "-o", "--output",
        default=OUTPUT_CSV,
        help=f"Output CSV filename (default: {OUTPUT_CSV})"
    )

    args = parser.parse_args()

    if args.start_time > args.end_time:
        parser.error("Start time must not be later than end time.")

    password = args.password
    if not password:
        password = getpass.getpass(prompt="Database password: ")

    conn = None

    try:
        conn = mariadb.connect(
            host=args.host,
            port=args.port,
            user=args.user,
            password=password,
            database=args.database,
        )

        cursor = conn.cursor()
        cursor.execute(QUERY, (args.start_time, args.end_time))

        columns = [col[0] for col in cursor.description]

        row_count = 0
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)

            for row in cursor:
                writer.writerow(row)
                row_count += 1

        print(f"Exported {row_count} records to {args.output}")

    except (mariadb.Error, KeyError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    finally:
        if conn is not None:
            conn.close()

if __name__ == "__main__":
    main()