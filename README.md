Driver Quiz — POD Training & Assessment System

A full-stack web application built for logistics operations to train and assess delivery drivers on POD (Proof of Delivery) compliance standards.

## Overview

This system was built to address a real operational need: drivers making POD errors needed targeted retraining based on their specific mistake category. Instead of a one-size-fits-all approach, this app assigns category-specific quizzes, tracks progress across multiple attempts, and escalates to randomized questions if a driver repeatedly fails.

## Features

- **Category-based quizzes** — 5 error categories (address issues, unclear labels, unsafe locations, etc.)
- **Progressive difficulty** — 5 fixed quiz sets; random question mode activates after repeated failures
- **Multilingual support** — Chinese, English, and Spanish
- **Admin dashboard** — assign quizzes, manage questions, view attempt history
- **Google Sheets integration** — auto-syncs quiz results to a shared sheet for ops teams
- **Multi-warehouse support** — covers 11 warehouse locations across the Southeast US
- **Question bank management** — admins can add, edit, preview, and publish questions

## Tech Stack

- **Backend:** Python, Flask, PostgreSQL
- **Frontend:** HTML, CSS, JavaScript (vanilla)
- **Integrations:** Google Sheets API (gspread), psycopg2
- **Auth:** Session-based admin and question bank login

## Getting Started

### 1. Clone the repo

```bash
git clone https://github.com/your-username/driver-quiz.git
cd driver-quiz
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up environment variables

Create a `.env` file in the root directory:

```env
SECRET_KEY=your-secret-key
DB_HOST=localhost
DB_PORT=5432
DB_NAME=driver_quiz_db
DB_USER=your_db_user
DB_PASSWORD=your_db_password
ADMIN_PASSWORD=your_admin_password
QUESTION_BANK_PASSWORD=your_qb_password
GOOGLE_SHEET_URL=your_google_sheet_url
GOOGLE_SHEET_TAB=quiz_results
GOOGLE_KEY_FILE=/path/to/your/google-service-account.json
```

### 4. Set up the database

```bash
psql -U your_db_user -c "CREATE DATABASE driver_quiz_db;"
```

The app auto-initializes all tables on first run.

### 5. Run the app

```bash
python app.py
```

Visit `http://localhost:5000`

## Project Structure

```
driver_quiz/
├── app.py                  # Flask app, routes, DB logic
├── templates/
│   ├── index.html          # Driver-facing quiz interface
│   ├── admin_dashboard.html
│   ├── admin_questions.html
│   ├── admin_question_form.html
│   ├── admin_assignment_new.html
│   └── ...
├── static/
│   ├── css/style.css
│   ├── css/admin.css
│   └── js/app.js
└── requirements.txt
```

## Background

This project was developed to solve a real workflow problem in a logistics environment. Drivers who failed POD audits previously had no structured retraining process. This system allowed operations managers to assign targeted quizzes by error category, track completion, and identify repeat offenders — reducing POD failure rates through consistent, measurable training.
