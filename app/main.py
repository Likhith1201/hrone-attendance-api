"""
Employee Attendance & Analytics API Full implementation 
"""
import os
import math
import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone, date as dt_date
from typing import Optional, List, Dict, Any, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup: Create Indexes Idempotently
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    """Create all indexes required for uniqueness, performance and explain plans."""
    # 1. employees: emp_code unique
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    # 2. employees: department filter & joined_on queries
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    # 3. attendance_logs: natural key unique index
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # 4. attendance_logs: listAttendance query (date DESC, emp_code ASC)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    # 5. attendance_logs: punch-out query (emp_code ASC, punch_in DESC)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


# --------------------------------------------------------------------------- #
# Helpers: Rounding & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: Optional[float], decimals: int = 2) -> Optional[float]:
    """R4, R8: half-up rounding (rates: 4 decimals half-up)."""
    if value is None:
        return None
    d = Decimal(str(value))
    exp = Decimal("10") ** (-decimals)
    return float(d.quantize(exp, rounding=ROUND_HALF_UP))


def epoch_ms_to_utc_datetime(ms: Optional[int]) -> Optional[datetime]:
    """Truncates milliseconds to whole seconds (R1) and returns UTC datetime."""
    if ms is None:
        return None
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a BSON or Python datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_ist_datetime(dt_utc: datetime) -> datetime:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(IST)


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in. Overnight shifts handle early punch-ins."""
    ist_dt = get_ist_datetime(punch_in_utc)
    ist_date = ist_dt.date()
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))

    if (eh_h, eh_m) <= (sh_h, sh_m):
        punch_time = (ist_dt.hour, ist_dt.minute, ist_dt.second)
        if punch_time < (eh_h, eh_m, 0):
            ist_date = ist_date - timedelta(days=1)

    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, attendance_date_str: str, shift_start: str) -> int:
    """R2: Grace period 10 min. Late if > 10:00 elapsed. Floor whole minutes from shift_start."""
    ist_dt = get_ist_datetime(punch_in_utc)
    att_date = dt_date.fromisoformat(attendance_date_str)
    sh_h, sh_m = map(int, shift_start.split(":"))
    shift_start_ist = datetime(att_date.year, att_date.month, att_date.day, sh_h, sh_m, 0, tzinfo=IST)

    diff_seconds = (ist_dt - shift_start_ist).total_seconds()
    if diff_seconds > 600:
        return int(math.floor(diff_seconds / 60))
    return 0


def compute_overtime_minutes(punch_out_utc: datetime, attendance_date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: Overtime >= 30 min past shift_end. Floor whole minutes from shift_end."""
    ist_out = get_ist_datetime(punch_out_utc)
    att_date = dt_date.fromisoformat(attendance_date_str)
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))

    end_date = att_date + timedelta(days=1) if (eh_h, eh_m) <= (sh_h, sh_m) else att_date
    shift_end_ist = datetime(end_date.year, end_date.month, end_date.day, eh_h, eh_m, 0, tzinfo=IST)

    diff_seconds = (ist_out - shift_end_ist).total_seconds()
    if diff_seconds >= 1800:
        return int(math.floor(diff_seconds / 60))
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def doc_to_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Transforms raw MongoDB attendance_logs doc into API schema response."""
    history_api = []
    for h in doc.get("history", []):
        h_changes = {}
        for k, v in h.get("changes", {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                from_val = datetime_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
                to_val = datetime_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
            h_changes[k] = {"from": from_val, "to": to_val}

        at_val = h.get("at")
        at_ms = datetime_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val
        history_api.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": h_changes
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_api
    }


def doc_to_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Transforms MongoDB employees doc into API schema response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"])
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("shift_end")
    @classmethod
    def check_shifts(cls, v, info):
        start = info.data.get("shift_start", "09:30")
        if v == start:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    created_at_utc = datetime.now(timezone.utc)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": created_at_utc
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return doc_to_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    query = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.employees.find(query).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [doc_to_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punch_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_in_utc = epoch_ms_to_utc_datetime(punch_ms)

    att_date = determine_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_utc, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for that date")

    return doc_to_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punch_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_out_utc = epoch_ms_to_utc_datetime(punch_ms)

    # Find the most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found for employee")

    # Race check & validation
    p_in = record.get("punch_in")
    if p_in.tzinfo is None:
        p_in = p_in.replace(tzinfo=timezone.utc)

    if punch_out_utc <= p_in or (punch_out_utc - p_in) > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="Invalid punch_out instant")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    # Derived calculations
    wh = compute_work_hours(p_in, punch_out_utc)
    ot = compute_overtime_minutes(punch_out_utc, record["date"], emp["shift_start"], emp["shift_end"])
    half_day = wh < 4.50

    # Atomic update to avoid punch-out race conditions
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punch_out_utc,
            "work_hours": wh,
            "overtime_minutes": ot,
            "half_day": half_day
        }},
        return_document=True
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record already punched out")

    return doc_to_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [doc_to_attendance_record(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]
    body_dict = body.model_dump(exclude_unset=True)

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in body_dict or "punch_out" in body_dict:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot have punch times")
        final_punch_in = None
        final_punch_out = None
    else:
        final_punch_in = epoch_ms_to_utc_datetime(body.punch_in) if "punch_in" in body_dict else record.get("punch_in")
        final_punch_out = epoch_ms_to_utc_datetime(body.punch_out) if "punch_out" in body_dict else record.get("punch_out")

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        if final_punch_in.tzinfo is None:
            final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)

        # punch_in must stay on the record's attendance date
        computed_date = determine_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(status_code=422, detail="punch_in does not match record attendance date")

        if final_punch_out is not None:
            if final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)
            if final_punch_out <= final_punch_in or (final_punch_out - final_punch_in) > timedelta(hours=24):
                raise HTTPException(status_code=422, detail="Invalid punch_out relative to punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_work_hours = None
        final_half_day = False
    else:
        final_late_minutes = compute_late_minutes(final_punch_in, date, emp["shift_start"])
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime_minutes(final_punch_out, date, emp["shift_start"], emp["shift_end"])
            final_half_day = final_work_hours < 4.50
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    # Check for actual changes
    changes = {}
    candidate_changes = [
        ("status", record.get("status"), new_status),
        ("punch_in", record.get("punch_in"), final_punch_in),
        ("punch_out", record.get("punch_out"), final_punch_out),
        ("work_hours", record.get("work_hours"), final_work_hours),
        ("late_minutes", record.get("late_minutes", 0), final_late_minutes),
        ("overtime_minutes", record.get("overtime_minutes", 0), final_overtime_minutes),
        ("half_day", record.get("half_day", False), final_half_day),
    ]

    for k, old_v, new_v in candidate_changes:
        if old_v != new_v:
            changes[k] = {"from": old_v, "to": new_v}

    if not changes:
        raise HTTPException(status_code=422, detail="No changes detected")

    now_utc = datetime.now(timezone.utc)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Concurrent safe update
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": record.get("history", [])},
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day
            },
            "$push": {"history": history_entry}
        },
        return_document=True
    )

    if not res:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return doc_to_attendance_record(res)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, mon = map(int, month.split("-"))
    next_month = dt_date(year + (1 if mon == 12 else 0), 1 if mon == 12 else mon + 1, 1)
    last_day_of_month = next_month - timedelta(days=1)
    first_day_of_month = dt_date(year, mon, 1)

    joined_date = dt_date.fromisoformat(emp["joined_on"])
    start_date = max(first_day_of_month, joined_date)

    working_days = 0
    if start_date <= last_day_of_month:
        curr = start_date
        while curr <= last_day_of_month:
            if curr.weekday() < 5:  # Monday to Friday
                working_days += 1
            curr += timedelta(days=1)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {"$project": {
            "status": 1,
            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            "half_day": {"$ifNull": ["$half_day", False]},
            "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]}  # Mon-Fri (MongoDB 1=Sun, 7=Sat)
                            ]
                        },
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"}
        }}
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        stats = agg_res[0]
        present_days = stats["present_days"]
        leave_days = stats["leave_days"]
        late_count = stats["late_count"]
        tot_late = stats["total_late_minutes"]
        tot_ot = stats["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        tot_late = 0
        tot_ot = 0

    attendance_pct = round_half_up((present_days / working_days * 100), 2) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": tot_late,
        "total_overtime_minutes": tot_ot,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    year, mon = map(int, month.split("-"))
    next_month = dt_date(year + (1 if mon == 12 else 0), 1 if mon == 12 else mon + 1, 1)
    last_day_str = (next_month - timedelta(days=1)).isoformat()

    match_stage = {"joined_on": {"$lte": last_day_str}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$emp"]},
                            {"$eq": [{"$substrCP": ["$date", 0, 7]}, month]}
                        ]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$in": [
                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                    [2, 3, 4, 5, 6]
                                ]}
                            ]
                        },
                        {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "valid_work_hours": {
                "$push": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$logs.work_hours", None]}
                            ]
                        },
                        "$logs.work_hours",
                        "$$REMOVE"
                    ]
                }
            },
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
        }},
        {"$project": {
            "department": "$_id",
            "headcount": {"$size": "$employees"},
            "present_days": 1,
            "avg_work_hours": {"$avg": "$valid_work_hours"},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1
        }},
        {"$sort": {"department": 1}}
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        avg_wh = round_half_up(r.get("avg_work_hours"), 2) if r.get("avg_work_hours") is not None else None
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": r["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"]
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    pipeline = [
        {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1
        }},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="from must be <= to")

    d_from = dt_date.fromisoformat(from_date)
    d_to = dt_date.fromisoformat(to_date)
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # Check if department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$emp"]},
                            {"$gte": ["$date", from_date]},
                            {"$lte": ["$date", to_date]}
                        ]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$logs.date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}}
        }},
        {"$match": {"_id": {"$ne": None}}},
        {"$project": {
            "date": "$_id",
            "present_count": 1,
            "late_count": 1,
            "d_date": {"$dateFromString": {"dateString": "$_id"}}
        }},
        {"$densify": {
            "field": "d_date",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [
                    datetime.combine(d_from, datetime.min.time(), tzinfo=timezone.utc),
                    datetime.combine(d_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
                ]
            }
        }},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d_date"}},
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$day"]}
                        ]
                    }
                }},
                {"$count": "count"}
            ],
            "as": "hc_info"
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_info.count", 0]}, 0]},
            "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
        }},
        {"$addFields": {
            "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]}
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, "current"]}
                }
            }
        }},
        {"$sort": {"date": 1}}
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        rate = round_half_up(r.get("attendance_rate"), 4) if r.get("attendance_rate") is not None else None
        m_avg = round_half_up(r.get("moving_avg_7d"), 4) if r.get("moving_avg_7d") is not None else None
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": r["present_count"],
            "late_count": r["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d": m_avg
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status:
            query["status"] = status
        skip = (page - 1) * page_size
        plan = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
            {"$group": {"_id": None, "total_late": {"$sum": "$late_minutes"}}}
        ]
        plan = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        match_stage = {"joined_on": {"$lte": f"{month}-31"}}
        if department:
            match_stage["department"] = department
        pipeline = [{"$match": match_stage}, {"$group": {"_id": "$department"}}]
        plan = db.command("aggregate", "employees", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = [
            {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late": {"$sum": "$late_minutes"}}}
        ]
        plan = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        pipeline = [{"$match": {"department": department}}]
        plan = db.command("aggregate", "employees", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}