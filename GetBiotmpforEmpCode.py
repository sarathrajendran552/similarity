#!/usr/bin/env python3

import sys
import argparse
import getpass
import mariadb

QUERY = """
SELECT b.bio_tmp
FROM personnel_employee AS p
JOIN iclock_biodata AS b
    ON b.employee_id = p.id
WHERE p.emp_code = ?
"""

def parse_args():
    parser = argparse.ArgumentParser(
        description="Export biometric template(s) for an employee."
    )
    
    # Database connection arguments (password is no longer required here)
    parser.add_argument("--host", required=True, help="Database hostname")
    parser.add_argument("--port", type=int, default=3306, help="Database port")
    parser.add_argument("--user", required=True, help="Database username")
    parser.add_argument("--password", help="Database password (will prompt if omitted)")
    parser.add_argument("--database", required=True, help="Database name")
    
    # Positional and output arguments
    parser.add_argument("emp_code", help="Employee code")
    parser.add_argument(
        "-o", "--output",
        default="biooutput",
        help="Output filename (default: biooutput)"
    )
    
    return parser.parse_args()

def main():
    args = parse_args()
    
    # If password wasn't passed as an argument, prompt for it securely
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
        cursor.execute(QUERY, (args.emp_code,))
        rows = cursor.fetchall()

        if not rows:
            print(f"No biometric template found for emp_code {args.emp_code}")
            sys.exit(1)

        with open(args.output, "wb") as f:
            for (bio_tmp,) in rows:
                if bio_tmp is None:
                    continue

                if isinstance(bio_tmp, str):
                    f.write(bio_tmp.encode("utf-8"))
                else:
                    f.write(bio_tmp)

                f.write(b"\n")

        print(f"Exported {len(rows)} template(s) to {args.output}")

    except (mariadb.Error, KeyError, OSError, ValueError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    finally:
        if conn is not None:
            conn.close()

if __name__ == "__main__":
    main()