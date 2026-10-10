"""
Employee Attendance & Analytics API.

The assignment keeps application code in this module. The code is split into
validation, business-rule helpers, CRUD, and MongoDB aggregation sections.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from bson import Decimal128, ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
IST_NAME = "Asia/Kolkata"
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MAX_EPOCH_MILLIS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     tz_aware=True, serverSelectionTimeoutMS=5_000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
attendance_logs = db.attendance_logs
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# Validation
EpochMillis = StrictInt


def _parse_date(value: str, field_name: str = "date") -> date:
    if not DATE_RE.fullmatch(value):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"{field_name} must be a valid calendar date") from exc


def _parse_month(value: str) -> tuple[date, date]:
    if not MONTH_RE.fullmatch(value):
        raise HTTPException(422, "month must be YYYY-MM")
    start = date.fromisoformat(f"{value}-01")
    next_month = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    return start, next_month - timedelta(days=1)


def _parse_shift(value: str, field_name: str) -> None:
    if not TIME_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be HH:MM")


def _validate_epoch(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be an integer epoch millisecond value")
    if not 100_000_000_000 <= value <= MAX_EPOCH_MILLIS:
        raise ValueError("must be epoch milliseconds between 100000000000 and 4102444800000")
    return value


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start")
    @classmethod
    def valid_shift_start(cls, value: str) -> str:
        _parse_shift(value, "shift_start")
        return value

    @field_validator("shift_end")
    @classmethod
    def valid_shift_end(cls, value: str) -> str:
        _parse_shift(value, "shift_end")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        _parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def different_shift_times(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return None if value is None else _validate_epoch(value)


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return None if value is None else _validate_epoch(value)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_punch_time(cls, value: Optional[int]) -> Optional[int]:
        return None if value is None else _validate_epoch(value)


# Time and attendance rules
def _utc_datetime_from_millis(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def _millis_from_datetime(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(((value.astimezone(UTC) - EPOCH).total_seconds()) * 1000)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _truncate_second(value: datetime) -> datetime:
    return _as_utc(value).replace(microsecond=0)


def _shift_minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _shift_start_datetime(record_date: str, shift_start: str) -> datetime:
    return datetime.combine(_parse_date(record_date), time.fromisoformat(shift_start), tzinfo=IST).astimezone(UTC)


def _shift_end_datetime(record_date: str, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(_parse_date(record_date), time.fromisoformat(shift_end), tzinfo=IST)
    if _shift_minutes(shift_end) <= _shift_minutes(shift_start):
        end += timedelta(days=1)
    return end.astimezone(UTC)


def _attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = _as_utc(punch_in).astimezone(IST)
    result = local.date()
    if _shift_minutes(shift_end) <= _shift_minutes(shift_start):
        if local.time().replace(second=0, microsecond=0) < time.fromisoformat(shift_end):
            result -= timedelta(days=1)
    return result.isoformat()


def _compute_late_minutes(punch_in: datetime, record_date: str, shift_start: str) -> int:
    elapsed_seconds = int((_as_utc(punch_in) - _shift_start_datetime(record_date, shift_start)).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def _compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    seconds = int((_as_utc(punch_out) - _as_utc(punch_in)).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _compute_overtime(punch_out: datetime, record_date: str, shift_start: str, shift_end: str) -> int:
    elapsed_seconds = int((_as_utc(punch_out) - _shift_end_datetime(record_date, shift_start, shift_end)).total_seconds())
    minutes = elapsed_seconds // 60
    return minutes if minutes >= 30 else 0


def _derived_values(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                    record_date: str, employee: dict[str, Any]) -> dict[str, Any]:
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = _compute_late_minutes(punch_in, record_date, employee["shift_start"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = _compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": float(hours),
        "late_minutes": late,
        "overtime_minutes": _compute_overtime(punch_out, record_date, employee["shift_start"], employee["shift_end"]),
        "half_day": hours < Decimal("4.50"),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return _millis_from_datetime(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _attendance_response(doc: dict[str, Any]) -> dict[str, Any]:
    result = dict(doc)
    result.pop("_id", None)
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    return _jsonable(result)


def _employee_response(doc: dict[str, Any]) -> dict[str, Any]:
    result = dict(doc)
    result.pop("_id", None)
    return _jsonable(result)


def _error(status_code: int, detail: str) -> None:
    raise HTTPException(status_code, detail)


def _round_reported(value: Optional[float], places: int = 2) -> Optional[float]:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


# Startup and indexes
def _create_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="employees_department_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="employees_department_joined")
    employees.create_index([("joined_on", ASCENDING)], name="employees_joined")
    attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_code")
    attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_status")


@app.on_event("startup")
def startup() -> None:
    try:
        _create_indexes()
    except PyMongoError:
        pass


# Basic endpoints
@app.get("/health")
def health() -> dict[str, str]:
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    document = body.model_dump()
    document["created_at"] = datetime.now(UTC)
    try:
        employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return _employee_response(document)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = employees.count_documents(query)
    cursor = (employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [_employee_response(doc) for doc in cursor], "total": total,
            "page": page, "page_size": page_size}


# Aggregation pipeline builders
def _month_bounds(month: str) -> tuple[str, str, date, date]:
    start, end = _parse_month(month)
    next_month = end + timedelta(days=1)
    return start.isoformat(), next_month.isoformat(), start, end


def _date_obj(field: str) -> dict[str, Any]:
    return {"$dateFromString": {"dateString": field, "timezone": IST_NAME}}


def _weekday(field: str) -> dict[str, Any]:
    return {"$dayOfWeek": {"date": field, "timezone": IST_NAME}}


def _employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    start, end_exclusive, _, _ = _month_bounds(month)
    month_start_expr = {"$dateFromString": {"dateString": start, "timezone": IST_NAME}}
    end_expr = {"$dateFromString": {"dateString": end_exclusive, "timezone": IST_NAME}}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {
            "period_start": {"$cond": [{"$gt": ["$joined_on", start]}, _date_obj("$joined_on"), month_start_expr]}
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code", "joined": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]},
                    {"$lt": ["$date", end_exclusive]}, {"$gte": ["$date", "$$joined"]}
                ]}}},
                {"$set": {"weekday": _weekday(_date_obj("$date"))}},
            ],
            "as": "logs",
        }},
        {"$set": {
            "working_days": {"$size": {"$filter": {
                "input": {"$map": {
                    "input": {"$range": [0, {"$dateDiff": {
                        "startDate": "$period_start", "endDate": end_expr, "unit": "day"
                    }}]},
                    "as": "offset",
                    "in": {"$dateAdd": {"startDate": "$period_start", "unit": "day", "amount": "$$offset"}},
                }},
                "as": "day_value",
                "cond": {"$and": [
                    {"$gte": [_weekday("$$day_value"), 2]}, {"$lte": [_weekday("$$day_value"), 6]}
                ]},
            }}},
            "present_days": {"$reduce": {
                "input": "$logs", "initialValue": 0,
                "in": {"$add": ["$$value", {"$cond": [
                    {"$and": [
                        {"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
                        {"$gte": ["$$this.weekday", 2]}, {"$lte": ["$$this.weekday", 6]}
                    ]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$$this.half_day", False]}, True]}, 0.5, 1]},
                    0,
                ]}]},
            }},
            "leave_days": {"$size": {"$filter": {
                "input": "$logs", "as": "log", "cond": {"$eq": ["$$log.status", "LEAVE"]}
            }}},
            "late_count": {"$size": {"$filter": {
                "input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}
            }}},
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "total_overtime_minutes": {"$sum": "$logs.overtime_minutes"},
        }},
        {"$project": {"_id": 0, "emp_code": 1, "working_days": 1, "present_days": 1,
                      "leave_days": 1, "late_count": 1, "total_late_minutes": 1,
                      "total_overtime_minutes": 1}},
    ]


def _department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict[str, Any]]:
    start, end_exclusive, _, month_end = _month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code", "joined": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]},
                    {"$lt": ["$date", end_exclusive]}, {"$gte": ["$date", "$$joined"]}
                ]}}},
                {"$set": {"weekday": _weekday(_date_obj("$date"))}},
            ],
            "as": "logs",
        }},
        {"$set": {"stats": {"$reduce": {
            "input": "$logs",
            "initialValue": {"present_days": 0, "hours_sum": 0, "hours_count": 0,
                             "late_count": 0, "late_minutes": 0, "leave_count": 0, "on_duty_count": 0},
            "in": {
                "present_days": {"$add": ["$$value.present_days", {"$cond": [
                    {"$and": [
                        {"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
                        {"$gte": ["$$this.weekday", 2]}, {"$lte": ["$$this.weekday", 6]}
                    ]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$$this.half_day", False]}, True]}, 0.5, 1]}, 0
                ]}]},
                "hours_sum": {"$add": ["$$value.hours_sum", {"$cond": [
                    {"$and": [
                        {"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
                        {"$ne": [{"$ifNull": ["$$this.work_hours", None]}, None]}
                    ]}, "$$this.work_hours", 0
                ]}]},
                "hours_count": {"$add": ["$$value.hours_count", {"$cond": [
                    {"$and": [
                        {"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
                        {"$ne": [{"$ifNull": ["$$this.work_hours", None]}, None]}
                    ]}, 1, 0
                ]}]},
                "late_count": {"$add": ["$$value.late_count", {"$cond": [
                    {"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0
                ]}]},
                "late_minutes": {"$add": ["$$value.late_minutes", {"$ifNull": ["$$this.late_minutes", 0]}]},
                "leave_count": {"$add": ["$$value.leave_count", {"$cond": [
                    {"$eq": ["$$this.status", "LEAVE"]}, 1, 0
                ]}]},
                "on_duty_count": {"$add": ["$$value.on_duty_count", {"$cond": [
                    {"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0
                ]}]},
            },
        }}}},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": "$stats.present_days"},
            "hours_sum": {"$sum": "$stats.hours_sum"}, "hours_count": {"$sum": "$stats.hours_count"},
            "late_count": {"$sum": "$stats.late_count"}, "total_late_minutes": {"$sum": "$stats.late_minutes"},
            "leave_count": {"$sum": "$stats.leave_count"}, "on_duty_count": {"$sum": "$stats.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$hours_count", 0]}, {"$divide": ["$hours_sum", "$hours_count"]}, None
            ]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
    ]


def _late_leaderboard_pipeline(month: str, department: Optional[str] = None) -> list[dict[str, Any]]:
    start, end_exclusive, _, _ = _month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lt": end_exclusive}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1,
                      "department": 1, "total_late_minutes": 1, "late_count": 1}},
    ])
    return pipeline


def _department_trend_pipeline(department: str, from_date: date, to_date: date) -> list[dict[str, Any]]:
    start_dt = datetime.combine(from_date, time.min, tzinfo=IST).astimezone(UTC)
    end_dt = datetime.combine(to_date, time.min, tzinfo=IST).astimezone(UTC)
    end_exclusive_dt = end_dt + timedelta(days=1)
    start_str, end_str = from_date.isoformat(), (to_date + timedelta(days=1)).isoformat()
    return [
        {"$match": {"date": {"$gte": start_str, "$lt": end_str}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": [{"$match": {"department": department}}], "as": "employee"}},
        {"$unwind": "$employee"},
        {"$set": {"day": _date_obj("$date")}},
        {"$project": {
            "day": 1,
            "present_count": {"$cond": [
                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0
            ]},
            "late_count": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]},
        }},
        {"$unionWith": {"coll": "attendance_logs", "pipeline": [
            {"$documents": [{"day": start_dt}, {"day": end_dt}]}
        ]}},
        {"$group": {"_id": "$day", "present_count": {"$sum": {"$ifNull": ["$present_count", 0]}},
                    "late_count": {"$sum": {"$ifNull": ["$late_count", 0]}}}},
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "day", "range": {
            "step": 1, "unit": "day", "bounds": [start_dt, end_exclusive_dt]
        }}},
        {"$set": {"present_count": {"$ifNull": ["$present_count", 0]},
                  "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {
            "from": "employees",
            "let": {"day_string": {"$dateToString": {
                "format": "%Y-%m-%d", "date": "$day", "timezone": IST_NAME
            }}},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day_string"]}
            ]}}}],
            "as": "headcount_employees",
        }},
        {"$set": {
            "headcount": {"$size": "$headcount_employees"},
            "is_working_day": {"$and": [
                {"$gte": [_weekday("$day"), 2]}, {"$lte": [_weekday("$day"), 6]}
            ]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
                                         "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day", "timezone": IST_NAME}},
            "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    employee = employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        _error(404, "employee not found")
    punched_at = _truncate_second(
        datetime.now(UTC) if body.punched_at is None else _utc_datetime_from_millis(body.punched_at)
    )
    record_date = _attendance_date(punched_at, employee["shift_start"], employee["shift_end"])
    document = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **_derived_values(body.status, punched_at, None, record_date, employee),
        "history": [],
    }
    try:
        attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return _attendance_response(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    employee = employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        _error(404, "employee not found")
    punched_at = _truncate_second(
        datetime.now(UTC) if body.punched_at is None else _utc_datetime_from_millis(body.punched_at)
    )
    candidate = attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if candidate is None:
        _error(404, "no punch-in found")
    if candidate.get("punch_out") is not None:
        _error(409, "attendance record is already punched out")
    if punched_at <= _as_utc(candidate["punch_in"]):
        _error(422, "punched_at must be after punch_in")
    if punched_at > _as_utc(candidate["punch_in"]) + timedelta(hours=24):
        _error(422, "punched_at must be within 24 hours of punch_in")
    derived = _derived_values(candidate["status"], candidate["punch_in"], punched_at,
                              candidate["date"], employee)
    updated = attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record is already punched out")
    return _attendance_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    if date_from is not None:
        _parse_date(date_from, "date_from")
    if date_to is not None:
        _parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        _error(422, "date_from must not be after date_to")
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    total = attendance_logs.count_documents(query)
    cursor = (attendance_logs.find(query, {"_id": 0})
              .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [_attendance_response(doc) for doc in cursor], "total": total,
            "page": page, "page_size": page_size}


# Manual corrections
def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, datetime) and isinstance(right, datetime):
        return _as_utc(left) == _as_utc(right)
    return left == right


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn) -> dict[str, Any]:
    record_date = _parse_date(date).isoformat()
    employee = employees.find_one({"emp_code": emp_code})
    if employee is None:
        _error(404, "employee not found")
    current = attendance_logs.find_one({"emp_code": emp_code, "date": record_date})
    if current is None:
        _error(404, "attendance record not found")

    supplied = body.model_fields_set
    if ("status" in supplied and body.status in ("ABSENT", "LEAVE")
            and ("punch_in" in supplied or "punch_out" in supplied)):
        _error(422, "ABSENT and LEAVE cannot include punch times")
    if (("punch_in" in supplied and body.punch_in is None)
            or ("punch_out" in supplied and body.punch_out is None)):
        _error(422, "punch times cannot be null")

    old_status = current.get("status")
    old_punch_in = current.get("punch_in")
    old_punch_out = current.get("punch_out")
    old_derived = {
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }
    new_status = body.status if "status" in supplied else old_status
    if new_status in ("ABSENT", "LEAVE") and ("punch_in" in supplied or "punch_out" in supplied):
        _error(422, "ABSENT and LEAVE cannot include punch times")
    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = new_punch_out = None
    else:
        new_punch_in = (_utc_datetime_from_millis(body.punch_in) if "punch_in" in supplied
                        else (None if old_punch_in is None else _truncate_second(old_punch_in)))
        new_punch_out = (_utc_datetime_from_millis(body.punch_out) if "punch_out" in supplied
                         else (None if old_punch_out is None else _truncate_second(old_punch_out)))

    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_punch_in is None:
        _error(422, "presence statuses require punch_in")
    if (new_punch_in is not None
            and _attendance_date(new_punch_in, employee["shift_start"], employee["shift_end"]) != record_date):
        _error(422, "punch_in must belong to the attendance date")
    if new_punch_out is not None:
        if new_punch_in is None or new_punch_out <= new_punch_in:
            _error(422, "punch_out must be after punch_in")
        if new_punch_out > new_punch_in + timedelta(hours=24):
            _error(422, "punch_out must be within 24 hours of punch_in")

    new_derived = _derived_values(new_status, new_punch_in, new_punch_out, record_date, employee)
    changes: dict[str, dict[str, Any]] = {}
    for field, old_value, new_value in (
        ("status", old_status, new_status),
        ("punch_in", old_punch_in, new_punch_in),
        ("punch_out", old_punch_out, new_punch_out),
        ("work_hours", old_derived["work_hours"], new_derived["work_hours"]),
        ("late_minutes", old_derived["late_minutes"], new_derived["late_minutes"]),
        ("overtime_minutes", old_derived["overtime_minutes"], new_derived["overtime_minutes"]),
        ("half_day", old_derived["half_day"], new_derived["half_day"]),
    ):
        if not _same_value(old_value, new_value):
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        _error(422, "request does not change the attendance record")

    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    set_values = {"status": new_status, "punch_in": new_punch_in, "punch_out": new_punch_out, **new_derived}
    concurrency_filter: dict[str, Any] = {"_id": current["_id"]}
    if "history" in current:
        concurrency_filter["history"] = current["history"]
    else:
        concurrency_filter["history"] = {"$exists": False}
    updated = attendance_logs.find_one_and_update(
        concurrency_filter,
        {"$set": set_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")
    return _attendance_response(updated)


# Analytics endpoints
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str) -> dict[str, Any]:
    _parse_month(month)
    if employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        _error(404, "employee not found")
    result = next(iter(employees.aggregate(_employee_monthly_pipeline(emp_code, month))), None)
    if result is None:
        _error(404, "employee not found")
    working_days = int(result.get("working_days", 0))
    pct = None if working_days == 0 else _round_reported(
        float(result.get("present_days", 0)) / working_days * 100
    )
    return _jsonable({
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": result.get("present_days", 0), "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0), "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0), "attendance_pct": pct,
    })


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None) -> dict[str, Any]:
    _parse_month(month)
    items = []
    for item in employees.aggregate(_department_summary_pipeline(month, department)):
        item["present_days"] = _round_reported(item.get("present_days", 0))
        item["avg_work_hours"] = _round_reported(item.get("avg_work_hours"))
        items.append(_jsonable(item))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None) -> dict[str, Any]:
    _parse_month(month)
    pipeline = _late_leaderboard_pipeline(month, department)
    pipeline.insert(-1, {"$match": {"rank": {"$lte": limit}}})
    return {"month": month, "items": [_jsonable(item) for item in attendance_logs.aggregate(pipeline)]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"),
                     to: str = Query(...)) -> dict[str, Any]:
    from_date = _parse_date(from_, "from")
    to_date = _parse_date(to, "to")
    if to_date < from_date:
        _error(422, "to must not be before from")
    if (to_date - from_date).days + 1 > 92:
        _error(422, "trend range must not exceed 92 days")
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        _error(404, "department not found")
    items = []
    for item in attendance_logs.aggregate(_department_trend_pipeline(department, from_date, to_date)):
        item["attendance_rate"] = _round_reported(item.get("attendance_rate"), 4)
        item["moving_avg_7d"] = _round_reported(item.get("moving_avg_7d"), 4)
        items.append(_jsonable(item))
    return {"department": department, "items": items}


# Explain endpoint
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    if endpoint == "attendance_list":
        if date_from:
            _parse_date(date_from, "date_from")
        if date_to:
            _parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            _error(422, "date_from must not be after date_to")
        query: dict[str, Any] = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status is not None:
            query["status"] = status
        explain = db.command("explain", {
            "find": "attendance_logs", "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size, "limit": page_size,
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _jsonable(explain)}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            _error(422, "emp_code and month are required")
        _parse_month(month)
        collection, pipeline = "employees", _employee_monthly_pipeline(emp_code, month)
    elif endpoint == "department_summary":
        if not month:
            _error(422, "month is required")
        _parse_month(month)
        collection, pipeline = "employees", _department_summary_pipeline(month, department)
    elif endpoint == "late_leaderboard":
        if not month:
            _error(422, "month is required")
        _parse_month(month)
        collection, pipeline = "attendance_logs", _late_leaderboard_pipeline(month, department)
        pipeline.insert(-1, {"$match": {"rank": {"$lte": limit}}})
    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            _error(422, "department, from and to are required")
        from_date, to_date = _parse_date(from_, "from"), _parse_date(to, "to")
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            _error(422, "invalid trend date range")
        collection, pipeline = "attendance_logs", _department_trend_pipeline(department, from_date, to_date)
    else:
        _error(422, "unknown endpoint")

    explain = db.command("explain", {
        "aggregate": collection, "pipeline": pipeline, "cursor": {}
    }, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": _jsonable(explain)}
