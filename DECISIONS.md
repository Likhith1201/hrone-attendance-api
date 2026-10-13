# DECISIONS.md

1. **Indexes.**
   - `employees: {emp_code: 1}` (unique) guarantees business-key uniqueness and fast lookups[cite: 1, 3].
   - `employees: {department: 1, joined_on: 1}` covers department filtering, headcount evaluation, and trend aggregations[cite: 3].
   - `attendance_logs: {emp_code: 1, date: 1}` (unique) enforces the one-record-per-day rule and backs `/attendance/{emp_code}/{date}`[cite: 1, 3].
   - `attendance_logs: {date: -1, emp_code: 1}` supports paginated listing without in-memory sorting[cite: 3].
   - `attendance_logs: {emp_code: 1, punch_in: -1}` backs punch-out lookups across midnight[cite: 3].
   - *Rejected:* Single-field index on `attendance_logs.status`; cardinality is low (5 values) and alone offers weak selectivity[cite: 1, 3].

2. **Punch-in race.**
   Both requests calculate identical attendance dates and attempt `insert_one`[cite: 1, 3]. The compound unique index on `(emp_code, date)` guarantees only one insertion commits[cite: 1, 3]. The second raises `DuplicateKeyError`, caught and converted into HTTP 409[cite: 3].

3. **Ties.**
   Using `$setWindowFields` with `$rank`, tied employees share identical ranks and subsequent ranks skip (e.g., 1, 2, 2, 4)[cite: 3]. Filtering with `rank <= limit` includes all tied candidates at the cutoff boundary, sorted secondary by `emp_code` ascending[cite: 3].

4. **Headcount.**
   The aggregation begins on `employees` filtered by `joined_on <= period_end`, followed by a `$lookup` into `attendance_logs`[cite: 3]. Even if an employee has zero logs, `$unwind` with `preserveNullAndEmptyArrays: true` preserves them in the department group[cite: 3].

5. **One thing you would change for 100x data.**
   Pre-aggregate monthly totals with scheduled incremental pipelines or change streams rather than running large multi-stage cross-collection `$lookup` operations on demand[cite: 3].