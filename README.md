# Employee Attendance and Analytics API

This repository contains the completed FastAPI and MongoDB implementation for the HROne assignment. The service implements the full contract: employee creation and paging, timezone-aware punch-in/punch-out rules, audited corrections, MongoDB aggregation analytics, and the explain endpoint.

## Run locally

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv
\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Set MONGO_URI and MONGO_DB in the environment, then load sample data if desired.
python sample_seed.py
uvicorn app.main:app --port 8000
```

MongoDB 6.0 or newer is required. The trend pipeline uses MongoDB gap filling and window functions, so MongoDB 7.0 is recommended.

The code is intentionally kept in app/main.py to match the assignment requirement. All timestamps at the API boundary are epoch milliseconds; calendar dates and shift times are IST values.

## Verification

```bash
python -m py_compile app/main.py
```

The service needs MongoDB for endpoint-level verification. No .env file, secrets, Dockerfile, or virtual environment belongs in the submission.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
