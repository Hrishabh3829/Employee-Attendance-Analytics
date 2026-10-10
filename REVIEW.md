# REVIEW.md

| # | Where | What is wrong | How you would notice it | How it was fixed |
|---|---|---|---|---|
| 1 | health | It always returns 200 without checking MongoDB. | Stop MongoDB and call /health. | Run a MongoDB ping and return 503 on failure. |
| 2 | Mongo client and timestamps | The client uses naive datetimes, so the host timezone changes the result. | Run the same request on a non-IST machine. | Use UTC-aware datetimes and convert calendar values through IST. |
| 3 | Request models | The starter models do not enforce the contract's patterns, ranges, enums, or strict epoch milliseconds. | Send a float, seconds-looking value, bad date, or invalid status. | Added Pydantic validation and explicit date/month parsing. |
| 4 | create_employee | It checks for a duplicate before inserting, which is a race, and creates no unique index. | Send concurrent creates for one emp_code. | Create a unique index and translate DuplicateKeyError to 409. |
| 5 | create_employee | created_at is a naive server datetime. | Inspect the response or run across timezones. | Store a UTC-aware server timestamp and serialize it as epoch milliseconds. |
| 6 | list_employees | skip starts at page * page_size, so page 1 is skipped. | Compare page 1 with the first employee. | Use (page - 1) * page_size. |
| 7 | list_employees | total ignores the department filter. | Request a filtered list and compare its count. | Count the same filtered query used for the page. |
| 8 | punch_in | An unknown employee is dereferenced after find_one, producing a 500. | Punch in with an unknown emp_code. | Return 404 before reading shift fields. |
| 9 | punch_in | The check-then-insert sequence is not race-safe. | Send simultaneous punch-ins for one employee/date. | Enforce the natural key with a unique index and catch the duplicate. |
| 10 | punch_in | It uses local naive time and date() instead of the IST/overnight attendance-date rule. | Punch in near midnight or on an overnight shift. | Normalize epoch milliseconds, convert to IST, and apply R1. |
| 11 | punch_in | It accepts any status and does not validate timestamp units. | Send ABSENT, a string timestamp, or seconds. | Use a presence-status enum and strict epoch validation. |
| 12 | compute_late_minutes | It builds shift start by replacing fields on the punch timestamp, so it is not an IST calendar calculation. | Test an overnight shift or a UTC host. | Build shift start from the record date in IST and apply the strict grace rule. |
| 13 | compute_work_hours | Python round uses binary floating point and ties-to-even behavior. | Test a duration whose second decimal is exactly halfway. | Use Decimal with ROUND_HALF_UP. |
| 14 | compute_overtime | It assumes a same-day shift end and does not apply the 30-minute threshold. | Punch out after midnight or 29 minutes after shift end. | Build overnight shift end correctly and floor/threshold elapsed minutes. |
| 15 | list_attendance | It loads the whole matching collection and sorts in Python. | Try it with roughly 100,000 records. | Use indexed Mongo sorting, count the filtered query, and paginate in MongoDB. |
| 16 | list_attendance | It mutates documents to add an id, leaks Mongo identity, and omits legacy defaults. | Inspect a response against the contract. | Project out _id, never add an id, and normalize missing history/half-day fields. |
| 17 | Missing endpoints | Punch-out, correction, all analytics, and explain were absent. | Compare starter routes with openapi.yaml. | Implemented every route in the contract, including aggregation pipelines. |

The original use of ISO YYYY-MM-DD strings for attendance-date filtering was not a defect: their lexical order is chronological. The natural employee key also correctly remains emp_code; MongoDB _id is an internal implementation detail.

