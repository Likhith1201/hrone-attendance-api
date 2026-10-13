# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` | Returns `{"status": "ok"}` statically without pinging MongoDB. If MongoDB is down, it falsely returns 200 instead of 503. | Stop MongoDB and hit `GET /health`; the API returns HTTP 200 instead of 503. | Wrapped database ping `client.admin.command("ping")` in a `try...except` block, returning 503 on failure. |
| 2 | `create_employee()` | Check-then-insert race condition (`find_one` followed by `insert_one`) without relying on a database unique index. | Send two concurrent `POST /employees` requests with identical `emp_code`; both succeed, corrupting uniqueness. | Created unique index on `employees.emp_code` at startup and handled `DuplicateKeyError` to return 409. |
| 3 | `create_employee()` | Uses `datetime.now()` (naive local server time) and stores/returns raw datetimes instead of epoch milliseconds. | Inspect returned `created_at` in response; returns ISO string / datetime rather than epoch milliseconds integer. | Used `datetime.now(timezone.utc)` and converted instants to integer epoch milliseconds in response. |
| 4 | `list_employees()` | Pagination bug: computes `skip = page * page_size`, which skips page 1 entirely when `page=1`. | Request `page=1, page_size=20`; the first 20 records are skipped. | Corrected offset formula to `(page - 1) * page_size`. |
| 5 | `list_employees()` | Total count bug: `total = db.employees.count_documents({})` counts the entire collection, ignoring the `department` filter. | Query `GET /employees?department=Engineering`; `total` returns total company headcount instead of department count. | Passed the filter query dictionary to `count_documents(query)`. |
| 6 | `punch_in()` | Race condition: uses `find_one` then `insert_one` to verify existing punch-in for the attendance date. | Fire concurrent punch-in calls at the same millisecond; duplicate records get created for the same date. | Added compound unique index on `("emp_code", "date")` and catch `DuplicateKeyError` to return 409. |
| 7 | `punch_in()` | Leaks internal identifier `"id"` derived from `_id` in response, violating schema. | Inspect JSON response: contains `"id": "<ObjectId>"` which is explicitly forbidden by contract. | Removed `"id"` field and shaped response strictly according to `AttendanceRecord` schema. |
| 8 | `list_attendance()` | Loads entire collection into memory (`list(db.attendance_logs.find(q))`) and sorts in Python, causing OOM at 100k docs. | Run benchmark with 100,000 records; endpoint causes high memory spikes and slow response times. | Used MongoDB cursor sorting `.sort([("date", -1), ("emp_code", 1)]).skip(...).limit(...)`. |
| 9 | `compute_late_minutes()` | Incorrect grace period calculation: does not enforce strictly greater than 10 minutes and computes naive delta. | A punch-in exactly at 09:40:00 or overnight shifts produces incorrect late minutes. | Used IST timezone conversion, strict `> 600` seconds check, and floored whole minutes since `shift_start`. |
| 10 | `compute_work_hours()` | Uses Python built-in `round()`, which applies banker's rounding (round-half-to-even) instead of round-half-up (R4). | Duration resulting in `x.xx5` rounds down to even rather than rounding up. | Used Python `Decimal` with `ROUND_HALF_UP` to round to 2 decimals. |
| 11 | `compute_overtime()` | Missing rule R3 requirements: does not enforce the 30-minute threshold or handle overnight shift calendar day rollover. | Punch-out 10 minutes after shift end gives 10 minutes overtime instead of 0. | Added minimum 30-minute threshold check (`>= 1800` seconds) and handled overnight shift end rollover. |

### Examined and Decided NOT a Defect

1. **Storing dates and shift times as plain strings (`YYYY-MM-DD`, `HH:MM`)**:
   - As explained in `DATA_MODEL.md`, calendar dates and shift times are intentionally strings to avoid timezone shift distortions across system boundaries.
2. **PyMongo default naive datetimes**:
   - Handled cleanly at the API boundary by explicitly treating naive datetimes from PyMongo as UTC instants before converting them to epoch milliseconds.