# Copyright 2026 Google LLC
# Seed script for TaskMaster Concierge Firestore database

import datetime
from google.cloud import firestore

FIRESTORE_PROJECT_ID = "qwiklabs-gcp-03-fe0b470d108a"


def seed_database():
    print(f"Connecting to Firestore with project ID: '{FIRESTORE_PROJECT_ID}'...")
    db = firestore.Client(project=FIRESTORE_PROJECT_ID)

    seed_items = [
        {
            "task_id": "task-001",
            "title": "Prepare Q3 Strategic Review",
            "description": "Draft executive summary slides and gather ROI metrics from team leads",
            "priority": "urgent_important",
            "status": "pending",
            "category": "Work",
            "due_date": "2026-09-28",
            "reminder_time": "2026-09-28 09:00 EDT",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        {
            "task_id": "task-002",
            "title": "Schedule Quarterly Health Checkup",
            "description": "Call clinic to confirm Friday morning appointment slot",
            "priority": "important",
            "status": "pending",
            "category": "Personal",
            "due_date": "2026-10-02",
            "reminder_time": "2026-10-01 14:00 EDT",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        {
            "task_id": "task-003",
            "title": "Review AI Architecture Newsletters",
            "description": "Catch up on latest model benchmark reports and paper releases",
            "priority": "neither",
            "status": "pending",
            "category": "Learning",
            "due_date": "2026-09-30",
            "reminder_time": "2026-09-30 17:00 EDT",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
    ]

    collection_ref = db.collection("tasks")
    for item in seed_items:
        doc_ref = collection_ref.document(item["task_id"])
        doc_ref.set(item)
        print(f"Seeded task: {item['task_id']} - {item['title']}")

    print("Firestore seeding complete!")


if __name__ == "__main__":
    seed_database()
