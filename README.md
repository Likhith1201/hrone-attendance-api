# Employee Attendance & Analytics API

A FastAPI backend service for employee attendance tracking and analytics, powered by MongoDB.

## Prerequisites

Ensure the following software is installed before running the application:

- **Python:** 3.11 or later
- **MongoDB:** 6.0 or later (local Docker container or MongoDB Atlas)

## Setup and Installation

### 1. Create and Activate a Virtual Environment

Create a Python virtual environment:

```bash
python -m venv .venv
```

Activate the virtual environment.

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\Activate.ps1
```

**Linux / macOS:**

```bash
source .venv/bin/activate
```

### 2. Install Dependencies

Install the required Python packages:

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Create a `.env` file in the project root directory and add the following configuration:

```ini
MONGO_URI=mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```

Replace `<username>`, `<password>`, and `<cluster>` with your MongoDB Atlas credentials and cluster details.

Alternatively, configure these variables directly in your system environment.

### 4. Load Sample Data (Optional)

To populate the database with sample employee attendance data, run:

```bash
python sample_seed.py
```

### 5. Start the Application

Run the FastAPI application using Uvicorn:

```bash
uvicorn app.main:app --port 8000
```

The application will be available at `http://localhost:8000`.

## API Endpoints

| Resource | URL | Description |
|---|---|---|
| API Base URL | `http://localhost:8000` | Base URL for API requests |
| Interactive API Documentation | `http://localhost:8000/docs` | Explore and test API endpoints |
| Health Check | `http://localhost:8000/health` | Check application health |

## Technologies Used

- **Python 3.11+**
- **FastAPI** — Backend API framework
- **MongoDB** — Database
- **Uvicorn** — ASGI server


