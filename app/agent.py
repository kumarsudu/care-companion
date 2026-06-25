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
import logging
import os
import re
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import AgentTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from google.adk.workflow import Workflow, START, node
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.genai import types

from .config import config

# Set up logger
logger = logging.getLogger("care_companion.security")

def log_security_event(severity: str, message: str, **kwargs):
    event = {
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "severity": severity,
        "message": message,
        **kwargs
    }
    print(f"AUDIT_LOG: {json.dumps(event)}")

# -----------------------------------------------------------------------------
# Nodes & Tools
# -----------------------------------------------------------------------------

def security_checkpoint(ctx: Context, node_input: types.Content) -> Event:
    """Security Checkpoint node: scrubs PII, checks prompt injection, logs events."""
    user_text = ""
    if node_input and node_input.parts:
        user_text = "".join([part.text for part in node_input.parts if part.text])

    # 1. PII scrubbing
    email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
    phone_pattern = r"\b(?:\+?1[-.●]?)?\(?([0-9]{3})\)?[-.●]?([0-9]{3})[-.●]?([0-9]{4})\b"
    ssn_pattern = r"\b\d{3}-\d{2}-\d{4}\b"

    cleaned_text = user_text
    cleaned_text = re.sub(email_pattern, "[REDACTED_EMAIL]", cleaned_text)
    cleaned_text = re.sub(phone_pattern, "[REDACTED_PHONE]", cleaned_text)
    cleaned_text = re.sub(ssn_pattern, "[REDACTED_SSN]", cleaned_text)

    # 2. Prompt injection checking
    injection_keywords = [
        "ignore instructions", "system prompt", "override rules",
        "jailbreak", "developer mode", "ignore prior instructions"
    ]
    is_injection = False
    for keyword in injection_keywords:
        if keyword in user_text.lower():
            is_injection = True
            break

    # 3. Domain-specific rule (no prescription requests or self-harm)
    blocked_keywords = [
        "prescription drugs without doctor", "buy illegal",
        "self-harm", "suicide", "kill myself"
    ]
    is_blocked = False
    for keyword in blocked_keywords:
        if keyword in user_text.lower():
            is_blocked = True
            break

    if is_injection:
        log_security_event(
            severity="CRITICAL",
            message="Prompt injection attempt detected.",
            user_input=user_text
        )
        return Event(
            output="Security alert: Prompt injection or rule override detected. Action blocked.",
            route="violation",
            state={"security_status": "injection_detected"}
        )

    if is_blocked:
        log_security_event(
            severity="WARNING",
            message="Domain-specific safety violation: request blocked.",
            user_input=user_text
        )
        return Event(
            output="Safety warning: If you are experiencing a medical emergency or crisis, please seek immediate help. CareCompanion cannot process self-harm queries or unauthorized medication transactions.",
            route="violation",
            state={"security_status": "safety_violation"}
        )

    # Log normal check pass
    log_security_event(
        severity="INFO",
        message="Security check passed.",
        pii_redacted=(cleaned_text != user_text)
    )

    return Event(
        output=cleaned_text,
        route="clean",
        state={"cleaned_input": cleaned_text}
    )


def security_violation_handler(node_input: str):
    """Outputs the security warning or block message to the UI."""
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=node_input)]))
    yield Event(output=node_input)


# Initialize Gemini Model
model = Gemini(
    model=config.model,
    retry_options=types.HttpRetryOptions(attempts=3),
)

# -----------------------------------------------------------------------------
# MCP Server Toolset Setup
# -----------------------------------------------------------------------------

current_dir = os.path.dirname(os.path.abspath(__file__))
mcp_server_path = os.path.join(current_dir, "mcp_server.py")

mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", mcp_server_path],
        ),
    ),
)

# -----------------------------------------------------------------------------
# Sub-agents (mode must be 'chat' because they are invoked via AgentTool)
# -----------------------------------------------------------------------------

symptom_analyzer = LlmAgent(
    name="symptom_analyzer",
    model=model,
    instruction="""You are CareCompanion's Symptom Analyzer.
Analyze the user's symptoms:
- Assess the potential severity and possible issues.
- Ask clarifying questions if needed.
- If the user asks about standard medications or side effects, use the get_medication_info tool to provide accurate details.
- ALWAYS include a safety disclaimer stating that you are an AI, not a doctor, and they should consult a professional.
""",
    description="Analyzes health symptoms, checks severity, and queries medication details.",
    tools=[mcp_toolset],
    mode="chat",
)

clinical_trial_advisor = LlmAgent(
    name="clinical_trial_advisor",
    model=model,
    instruction="""You are CareCompanion's Clinical Trial Advisor.
Search for and suggest relevant clinical trials using the available tools:
- Use search_clinical_trials to find trials matching the user's condition and optional location.
- Use get_trial_eligibility to check specific eligibility rules for a given trial ID.
- Summarize the eligibility criteria, phase, and location.
- Keep the recommendations clear and actionable.
""",
    description="Searches for and retrieves clinical trial information.",
    tools=[mcp_toolset],
    mode="chat",
)

# -----------------------------------------------------------------------------
# Orchestrator (mode must be 'single_turn' because it is a graph node)
# -----------------------------------------------------------------------------

orchestrator = LlmAgent(
    name="orchestrator",
    model=model,
    instruction="""You are the CareCompanion Lead Concierge.
Coordinate the user's request. You have two specialized assistants available as tools:
- symptom_analyzer: Use this to check and analyze health symptoms.
- clinical_trial_advisor: Use this to search clinical trials and medical studies.

Route the request to the appropriate assistant, synthesize their response, and present a helpful, professional concierge summary back to the user.
""",
    tools=[AgentTool(symptom_analyzer), AgentTool(clinical_trial_advisor)],
    mode="single_turn",
)


@node(rerun_on_resume=True)
def hitl_consent(ctx: Context, node_input: Any):
    """Human-in-the-loop consent check before returning clinical trial or symptom results."""
    # Convert node_input to string safely
    orchestrator_text = ""
    if isinstance(node_input, types.Content) and node_input.parts:
        orchestrator_text = "".join([part.text for part in node_input.parts if part.text])
    else:
        orchestrator_text = str(node_input)

    # Check if consent was already approved
    if ctx.state.get("user_consent") == "approved":
        yield Event(output=orchestrator_text)
        return

    # Trigger HITL if output contains trials or symptom analysis
    lower_text = orchestrator_text.lower()
    if "trial" in lower_text or "symptom" in lower_text:
        if not ctx.resume_inputs or "consent" not in ctx.resume_inputs:
            yield RequestInput(
                interrupt_id="consent",
                message="CareCompanion is an AI assistant, not a medical professional. Before displaying matching trials or symptom analysis, please confirm you consent to the storage of this query for search and verification. Do you consent? (Type 'yes' or 'no')"
            )
            return

        user_reply = ctx.resume_inputs.get("consent", "").strip().lower()
        if user_reply == "yes":
            yield Event(output=orchestrator_text, state={"user_consent": "approved"})
        else:
            yield Event(output="Consent declined. CareCompanion cannot display clinical trial or symptom results without consent.")
    else:
        yield Event(output=orchestrator_text)


def final_response(node_input: str):
    """Formats final response and yields content event for UI rendering."""
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=node_input)]))
    yield Event(output=node_input)

# -----------------------------------------------------------------------------
# Workflow Definition
# -----------------------------------------------------------------------------

workflow = Workflow(
    name="care_companion_workflow",
    edges=[
        ('START', security_checkpoint),
        (security_checkpoint, {"violation": security_violation_handler}),
        (security_checkpoint, {"clean": orchestrator}),
        (orchestrator, hitl_consent),
        (hitl_consent, final_response)
    ],
)

app = App(
    root_agent=workflow,
    name="app",
)
