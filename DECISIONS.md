# DECISIONS.md

1. I create a unique employee index on emp_code and a unique attendance index on (emp_code, date). The attendance date/code index serves filtered lists and their sort; the date/status index helps date/status scans. Employee department/code, department/joined_on, and joined_on indexes support list and headcount lookups. I considered a separate index on punch_in for punch-out, but the natural-key and date indexes cover the normal access paths and a new index would add write cost.

2. Two identical punch-ins both read the employee, build the same natural key, and attempt an insert. MongoDB's unique index accepts one and rejects the others with DuplicateKeyError, which the API maps to 409. There is no check-then-insert decision.

3. The leaderboard uses MongoDB rank over total late minutes. If the cutoff is rank 2 and two people tie at rank 2, both are returned; the next rank is 4. Rows are then ordered by total minutes descending and emp_code ascending.

4. Department summary starts from employees, filters by joined_on <= month end, then looks up logs. Grouping employees means a person with zero logs still contributes one to headcount and zero to the log metrics.

5. At 100x the data, I would move analytics to pre-aggregated daily summaries updated from an event stream, while retaining the raw attendance log for auditability.

