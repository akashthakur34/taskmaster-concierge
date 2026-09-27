# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import json
import os
import urllib.request
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from google import genai
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.memory import VertexAiMemoryBankService
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.cloud import firestore, storage
from google.genai import types

from app.a2ui_utils import a2ui_callback

# Hardcoded GCP Project ID and Cloud Storage bucket string as required
PROJECT_ID = "qwiklabs-gcp-03-fe0b470d108a"
GCS_BUCKET_NAME = "taskmaster-concierge-assets-qwiklabs-gcp-03-fe0b470d108a"

# Load Agent Engine resource name from deployment_metadata.json if available
deployment_metadata_path = Path(__file__).parent.parent / "deployment_metadata.json"
agent_engine_resource_name = None
memory_bank_id = "6225166005874720768"

if deployment_metadata_path.exists():
    try:
        with open(deployment_metadata_path, "r") as f:
            metadata = json.load(f)
            agent_engine_resource_name = metadata.get("remote_agent_runtime_id")
            if agent_engine_resource_name:
                memory_bank_id = agent_engine_resource_name.split("/")[-1]
    except Exception as e:
        print(f"Warning: Failed to load deployment_metadata.json: {e}")

# Instantiate AgentEngineSandboxCodeExecutor for secure sandboxed Python execution
code_executor = AgentEngineSandboxCodeExecutor(
    agent_engine_resource_name=agent_engine_resource_name
)


def memory_bank_service_builder():
    """Builds VertexAiMemoryBankService for deployed Agent Runtime memory service."""
    return VertexAiMemoryBankService(
        project=PROJECT_ID,
        location="us-central1",
        agent_engine_id=memory_bank_id,
    )


async def generate_memories_callback(callback_context: CallbackContext):
    """WRITE: After each agent turn, extract and send key facts/preferences to Memory Bank."""
    try:
        await callback_context.add_session_to_memory()
    except Exception as e:
        print(f"Memory Bank save skipped: {e}")
    return None


# Initialize GCP Clients
db = firestore.Client(project=PROJECT_ID)
storage_client = storage.Client(project=PROJECT_ID)
genai_client = genai.Client(vertexai=True, project=PROJECT_ID, location="us-central1")
genai_global_client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")


def list_tasks(status: str = "all", priority: str = "all") -> str:
    """Lists tasks from the Firestore tasks collection with optional filtering by status or priority.

    Args:
        status: Filter tasks by status ('all', 'pending', 'in_progress', 'completed').
        priority: Filter tasks by priority ('all', 'urgent_important', 'important', 'urgent', 'neither').

    Returns:
        A formatted string listing all matching tasks and their details.
    """
    collection_ref = db.collection("tasks")
    docs = collection_ref.stream()

    tasks = []
    for doc in docs:
        data = doc.to_dict()
        task_status = data.get("status", "pending")
        task_priority = data.get("priority", "neither")

        if status != "all" and task_status.lower() != status.lower():
            continue
        if priority != "all" and task_priority.lower() != priority.lower():
            continue

        tasks.append(
            f"• [{data.get('task_id', doc.id)}] {data.get('title')} | Status: {task_status} | Priority: {task_priority} | Category: {data.get('category', 'General')} | Due: {data.get('due_date', 'None')} | Reminder: {data.get('reminder_time', 'None')}\n  Details: {data.get('description', '')}"
        )

    if not tasks:
        return "No tasks found matching your filter criteria."

    return f"Found {len(tasks)} task(s):\n" + "\n\n".join(tasks)


def add_task(
    title: str,
    description: str = "",
    priority: str = "important",
    category: str = "Work",
    due_date: str = "",
    reminder_time: str = "",
) -> str:
    """Adds a new task to the Firestore tasks collection.

    Args:
        title: The headline or short title of the task.
        description: Detailed context or sub-steps for the task.
        priority: Priority rating according to the Eisenhower Matrix ('urgent_important', 'important', 'urgent', 'neither').
        category: Category tag ('Work', 'Personal', 'Health', 'Finance', 'Learning').
        due_date: Target completion date string (e.g. '2026-09-30').
        reminder_time: Scheduled reminder time string (e.g. '2026-09-30 09:00 EDT').

    Returns:
        Confirmation message with the created task ID.
    """
    task_id = f"task-{uuid.uuid4().hex[:6]}"
    doc_ref = db.collection("tasks").document(task_id)

    task_data = {
        "task_id": task_id,
        "title": title,
        "description": description,
        "priority": priority.lower(),
        "status": "pending",
        "category": category,
        "due_date": due_date,
        "reminder_time": reminder_time,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    doc_ref.set(task_data)
    return f"Task successfully created! ID: {task_id}, Title: '{title}', Priority: {priority}, Status: pending."


def update_task_priority(task_id: str, new_priority: str) -> str:
    """Updates the Eisenhower Matrix priority rating of an existing task in Firestore.

    Args:
        task_id: The unique ID of the task (e.g. 'task-001').
        new_priority: New priority rating ('urgent_important', 'important', 'urgent', 'neither').

    Returns:
        Confirmation message of the priority update.
    """
    doc_ref = db.collection("tasks").document(task_id)
    doc = doc_ref.get()

    if not doc.exists:
        return f"Error: Task with ID '{task_id}' was not found in Firestore."

    doc_ref.update({"priority": new_priority.lower()})
    return f"Priority for task '{task_id}' updated to '{new_priority.lower()}'."


def complete_task(task_id: str) -> str:
    """Marks a task as completed in Firestore.

    Args:
        task_id: The unique ID of the task (e.g. 'task-001').

    Returns:
        Confirmation message that the task status has been updated to completed.
    """
    doc_ref = db.collection("tasks").document(task_id)
    doc = doc_ref.get()

    if not doc.exists:
        return f"Error: Task with ID '{task_id}' was not found in Firestore."

    doc_ref.update({"status": "completed"})
    return f"Task '{task_id}' ({doc.to_dict().get('title')}) has been marked as completed! 🎉"


def set_reminder(task_id: str, reminder_time: str) -> str:
    """Sets or updates the scheduled reminder time for a task in Firestore.

    Args:
        task_id: The unique ID of the task (e.g. 'task-001').
        reminder_time: Target date/time string for the reminder (e.g. '2026-09-28 09:00 EDT').

    Returns:
        Confirmation message of the scheduled reminder.
    """
    doc_ref = db.collection("tasks").document(task_id)
    doc = doc_ref.get()

    if not doc.exists:
        return f"Error: Task with ID '{task_id}' was not found in Firestore."

    doc_ref.update({"reminder_time": reminder_time})
    return f"Reminder for task '{task_id}' set to '{reminder_time}'."


def calculate_workload_analytics() -> str:
    """Calculates workload analytics, focus hour estimates, and Eisenhower Matrix distribution across all tasks in Firestore.

    Returns:
        A detailed executive workload report with capacity and burnout risk metrics.
    """
    docs = db.collection("tasks").stream()

    total_tasks = 0
    completed_tasks = 0
    pending_tasks = 0

    quadrants = {
        "urgent_important": [],  # Q1
        "important": [],         # Q2
        "urgent": [],            # Q3
        "neither": [],           # Q4
    }

    hour_estimates = {
        "urgent_important": 3.0,
        "important": 2.0,
        "urgent": 1.5,
        "neither": 0.5,
    }

    total_focus_hours = 0.0

    for doc in docs:
        data = doc.to_dict()
        total_tasks += 1
        status = data.get("status", "pending")
        priority = data.get("priority", "neither").lower()

        if status == "completed":
            completed_tasks += 1
        else:
            pending_tasks += 1
            if priority in quadrants:
                quadrants[priority].append(data.get("title"))
                total_focus_hours += hour_estimates.get(priority, 1.0)
            else:
                quadrants["neither"].append(data.get("title"))
                total_focus_hours += 0.5

    completion_rate = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0
    q1_count = len(quadrants["urgent_important"])
    q2_count = len(quadrants["important"])
    q3_count = len(quadrants["urgent"])
    q4_count = len(quadrants["neither"])

    if (q1_count + q3_count) >= 4 or total_focus_hours > 15.0:
        burnout_risk = "HIGH ⚠️ (Heavy reactive load in Q1/Q3. Consider delegating or rescheduling Q3 tasks)."
    elif q2_count >= q1_count:
        burnout_risk = "OPTIMAL ✅ (Strong proactive focus on Q2 strategic goals)."
    else:
        burnout_risk = "MODERATE 🟡 (Manageable workload, stay attentive to deadlines)."

    report = (
        "📊 **Executive Workload & Productivity Analytics**\n\n"
        f"• Total Managed Tasks: {total_tasks}\n"
        f"• Pending Workload: {pending_tasks} task(s)\n"
        f"• Completed Tasks: {completed_tasks} ({completion_rate:.1f}% completion rate)\n"
        f"• Total Estimated Focus Time Required: ~{total_focus_hours:.1f} hours\n\n"
        "**Eisenhower Matrix Breakdown (Pending Workload)**:\n"
        f"  - Q1 (Urgent & Important): {q1_count} task(s)\n"
        f"  - Q2 (Important, Not Urgent): {q2_count} task(s)\n"
        f"  - Q3 (Urgent, Not Important): {q3_count} task(s)\n"
        f"  - Q4 (Neither): {q4_count} task(s)\n\n"
        f"**Capacity & Burnout Risk Indicator**: {burnout_risk}"
    )

    return report


def generate_task_artifact_image(
    task_title: str,
    tool_context: ToolContext,
    style_description: str = "sleek modern executive dark mode badge with gold accents",
) -> str:
    """Generates an image artifact for a task using gemini-3.1-flash-lite-image in the global region.
    Saves it as an ADK session artifact (so it appears in Playground's Artifacts panel)
    and uploads it to Cloud Storage, returning its public HTTPS URL.

    Args:
        task_title: The title or topic of the task to generate an image for.
        tool_context: ADK ToolContext instance passed by framework at runtime.
        style_description: Optional design or style theme description.

    Returns:
        The public Cloud Storage HTTPS URL of the uploaded image.
    """
    prompt = (
        f"A professional visual task badge image for executive task '{task_title}'. "
        f"Style: {style_description}. High quality, clean modern design."
    )

    response = genai_global_client.models.generate_content(
        model="gemini-3.1-flash-lite-image",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
        ),
    )

    image_bytes = None
    mime_type = "image/png"
    for part in response.candidates[0].content.parts:
        if part.inline_data:
            image_bytes = part.inline_data.data
            if part.inline_data.mime_type:
                mime_type = part.inline_data.mime_type
            break

    if not image_bytes:
        return "Error: Failed to generate image bytes from gemini-3.1-flash-lite-image."

    ext = "jpg" if "jpeg" in mime_type.lower() else "png"
    filename = f"task_artifact_{uuid.uuid4().hex[:6]}.{ext}"

    # 1. Save with tool_context.save_artifact for Playground Artifacts panel
    artifact_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    if tool_context:
        tool_context.save_artifact(filename=filename, artifact=artifact_part)

    # 2. Upload same bytes directly to public GCS bucket (hardcoded string)
    bucket = storage_client.bucket(GCS_BUCKET_NAME)
    blob = bucket.blob(f"artifacts/{filename}")
    blob.upload_from_string(image_bytes, content_type=mime_type)

    public_url = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/artifacts/{filename}"
    return public_url


def generate_task_artifact_video(
    task_title: str,
    tool_context: ToolContext,
    concept_description: str = "dynamic executive task video summary with golden motion graphics",
) -> str:
    """Generates a video artifact for a task using Google's Omni model (gemini-omni-flash-preview) in the global region.
    Saves it as an ADK session artifact (so it appears in Playground's Artifacts panel)
    and uploads it to Cloud Storage, returning its public HTTPS URL.

    Args:
        task_title: The title or topic of the task to generate a video for.
        tool_context: ADK ToolContext instance passed by framework at runtime.
        concept_description: Optional visual animation concept description.

    Returns:
        The public Cloud Storage HTTPS URL of the uploaded video.
    """
    prompt = (
        f"A short, high-quality executive video clip for task '{task_title}'. "
        f"Concept: {concept_description}. Professional motion graphics and dynamic visuals."
    )

    response = genai_global_client.models.generate_content(
        model="gemini-omni-flash-preview",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["VIDEO"],
        ),
    )

    video_bytes = None
    mime_type = "video/mp4"
    for part in response.candidates[0].content.parts:
        if part.inline_data:
            video_bytes = part.inline_data.data
            if part.inline_data.mime_type:
                mime_type = part.inline_data.mime_type
            break

    if not video_bytes:
        return "Error: Failed to generate video bytes from gemini-omni-flash-preview."

    ext = "webm" if "webm" in mime_type.lower() else "mp4"
    filename = f"task_video_{uuid.uuid4().hex[:6]}.{ext}"

    # 1. Save with tool_context.save_artifact for Playground Artifacts panel
    artifact_part = types.Part.from_bytes(data=video_bytes, mime_type=mime_type)
    if tool_context:
        tool_context.save_artifact(filename=filename, artifact=artifact_part)

    # 2. Upload same bytes directly to public GCS bucket (hardcoded string)
    bucket = storage_client.bucket(GCS_BUCKET_NAME)
    blob = bucket.blob(f"artifacts/{filename}")
    blob.upload_from_string(video_bytes, content_type=mime_type)

    public_url = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/artifacts/{filename}"
    return public_url


def generate_focus_banner(
    topic: str = "Executive Daily Focus",
    style_theme: str = "sleek modern dark mode with vibrant neon gold accents",
) -> str:
    """Generates a custom visual focus banner image using AI and saves it to a public Cloud Storage bucket.

    Args:
        topic: The main title or focus area for the banner (e.g. 'Q3 Strategic Review', 'Health & Wellness').
        style_theme: Visual style description (e.g. 'minimalist futuristic dark mode', 'vibrant executive desk').

    Returns:
        The public GCS URL of the generated image and embedding Markdown syntax.
    """
    prompt = (
        f"A sleek, high-resolution executive productivity banner image for '{topic}'. "
        f"Theme: {style_theme}. Professional, modern aesthetic, high quality 16:9 ratio."
    )

    response = genai_client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
        ),
    )

    image_bytes = None
    for part in response.candidates[0].content.parts:
        if part.inline_data:
            image_bytes = part.inline_data.data
            break

    if not image_bytes:
        return "Error: Failed to generate image bytes from model."

    bucket = storage_client.bucket(GCS_BUCKET_NAME)
    filename = f"banners/focus_banner_{uuid.uuid4().hex[:6]}.png"
    blob = bucket.blob(filename)
    blob.upload_from_string(image_bytes, content_type="image/png")

    public_url = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{filename}"
    return f"Successfully generated focus banner for '{topic}'!\nPublic Image URL: {public_url}\n\n![Focus Banner]({public_url})"


def fetch_daily_quote() -> str:
    """Fetches a real daily motivational quote and executive mindset insight from ZenQuotes Public API.

    Returns:
        A formatted string containing the quote and author.
    """
    try:
        api_key = os.getenv("ZENQUOTES_API_KEY", "")
        url = f"https://zenquotes.io/api/random/{api_key}" if api_key else "https://zenquotes.io/api/random"
        req = urllib.request.Request(url, headers={"User-Agent": "TaskMasterConcierge/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            if data and isinstance(data, list) and len(data) > 0:
                quote = data[0].get("q", "")
                author = data[0].get("a", "Unknown")
                return f"💡 **Executive Mindset & Focus Quote**:\n\"{quote}\"\n— *{author}*"
    except Exception as e:
        return f"Unable to fetch daily quote at this time: {str(e)}"
    return "No quote found."


def fetch_upcoming_holidays(year: int = 2026, country_code: str = "US") -> str:
    """Fetches real public holidays from Nager.Date Public API for calendar planning.

    Args:
        year: The calendar year (e.g. 2026).
        country_code: Two-letter ISO country code (e.g. 'US', 'GB', 'CA').

    Returns:
        A formatted string listing public holidays.
    """
    try:
        url = f"https://date.nager.at/api/v3/publicholidays/{year}/{country_code.upper()}"
        req = urllib.request.Request(url, headers={"User-Agent": "TaskMasterConcierge/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            holidays = json.loads(resp.read().decode())
            if not holidays:
                return f"No holidays found for {country_code} in {year}."

            formatted = [f"• {h.get('date')}: {h.get('name')}" for h in holidays[:8]]
            return f"📅 **Upcoming Public Holidays for {country_code.upper()} ({year})**:\n" + "\n".join(formatted)
    except Exception as e:
        return f"Unable to fetch public holidays at this time: {str(e)}"


def get_weather(query: str) -> str:
    """Simulates a web search for weather information.

    Args:
        query: A string containing the location to get weather information for.

    Returns:
        Simulated weather string for the queried location.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        return "It's 60 degrees and foggy."
    return "It's 90 degrees and sunny."


def get_current_time(query: str) -> str:
    """Simulates getting the current time for a city.

    Args:
        query: The name of the city to get the current time for.

    Returns:
        Current time string for the queried city.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        tz_identifier = "America/Los_Angeles"
    elif "ny" in query.lower() or "new york" in query.lower():
        tz_identifier = "America/New_York"
    else:
        return f"Sorry, I don't have timezone information for query: {query}."

    tz = ZoneInfo(tz_identifier)
    now = datetime.datetime.now(tz)
    return f"The current time for query {query} is {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"


# Build A2UI System Prompt with version 0.8 and Basic Catalog
schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

a2ui_instruction = schema_manager.generate_system_prompt(
    role_description=(
        "You are TaskMaster Concierge, an executive AI assistant specializing in productivity, "
        "task management, priority optimization (Eisenhower Matrix), workload analytics, calendar holiday planning, "
        "daily executive quotes, visual task artifacts, and custom focus banners. "
        "You have Python code execution enabled via AgentEngineSandboxCodeExecutor for secure sandboxed data computations, "
        "and cross-session long-term memory via Vertex AI Memory Bank."
    ),
    workflow_description=(
        "When the user asks to list, view, or check tasks, ALWAYS call the `list_tasks` tool first "
        "to fetch the latest tasks directly from Firestore before constructing your response."
    ),
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        '{"Image": {"url": {"literalString": "https://..."}}}. Never point an '
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects."
    ),
    include_schema=True,
    include_examples=True,
)


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=a2ui_instruction,
    code_executor=code_executor,
    tools=[
        PreloadMemoryTool(),
        list_tasks,
        add_task,
        update_task_priority,
        complete_task,
        set_reminder,
        calculate_workload_analytics,
        generate_task_artifact_image,
        generate_task_artifact_video,
        generate_focus_banner,
        fetch_daily_quote,
        fetch_upcoming_holidays,
        get_current_time,
        get_weather,
    ],
    after_agent_callback=generate_memories_callback,
    after_model_callback=a2ui_callback,
)

app = App(
    root_agent=root_agent,
    name="app",
)
