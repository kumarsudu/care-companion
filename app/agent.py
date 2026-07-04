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

# CareCompanion — Personal Health Concierge Agent
#
# Architecture overview:
#   START → security_checkpoint ─┬─(violation)─→ security_violation_handler → END
#                                 └─(clean)─────→ orchestrator → hitl_consent → final_response → END
#
# Design principles:
#   1. No LLM is ever invoked before the security checkpoint runs.
#      This is enforced structurally by the Workflow graph, not by prompt instructions.
#   2. Sub-agents (symptom_analyzer, clinical_trial_advisor) are isolated by domain.
#      The orchestrator delegates via AgentTool — it never reasons about health specifics.
#   3. The HITL consent gate ensures no health data is surfaced without explicit user approval.

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

logger = logging.getLogger("care_companion.security")


# -----------------------------------------------------------------------------
# Audit Logging
# -----------------------------------------------------------------------------

def log_security_event(severity: str, message: str, **kwargs):
    """Emit a structured JSON audit event to stdout for production log ingestion.

    Severity levels:
      INFO     — normal pass-through (PII may have been redacted)
      WARNING  — domain safety rule triggered (e.g., self-harm, illegal purchase)
      CRITICAL — prompt injection attempt detected
    """
    event = {
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "severity": severity,
        "message": message,
        **kwargs
    }
    print(f"AUDIT_LOG: {json.dumps(event)}")


# -----------------------------------------------------------------------------
# Security Checkpoint Node
# -----------------------------------------------------------------------------

def security_checkpoint(ctx: Context, node_input: types.Content) -> Event:
    """First node in the workflow graph — runs before any LLM sees user input.

    Performs three sequential, deterministic checks (no LLM involved):
      1. PII scrubbing   — redacts emails, phone numbers, SSNs in-place
      2. Injection guard — blocks known prompt-override keywords
      3. Domain safety   — blocks self-harm and unauthorized medication requests

    Returns an Event routed to either 'violation' or 'clean'.
    The 'cleaned_input' state key carries the redacted text to downstream nodes.
    """
    user_text = ""
    if node_input and node_input.parts:
        user_text = "".join([part.text for part in node_input.parts if part.text])

    # --- Layer 1: PII Scrubbing ---
    # Redact before any storage or model call to minimize exposure surface.
    email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
    phone_pattern = r"\b(?:\+?1[-.●]?)?\(?([0-9]{3})\)?[-.●]?([0-9]{3})[-.●]?([0-9]{4})\b"
    ssn_pattern = r"\b\d{3}-\d{2}-\d{4}\b"

    cleaned_text = user_text
    cleaned_text = re.sub(email_pattern, "[REDACTED_EMAIL]", cleaned_text)
    cleaned_text = re.sub(phone_pattern, "[REDACTED_PHONE]", cleaned_text)
    cleaned_text = re.sub(ssn_pattern, "[REDACTED_SSN]", cleaned_text)

    # --- Layer 2: Prompt Injection Detection ---
    # Adversarial users may try to override system instructions via the chat input.
    # These keywords are matched case-insensitively against the original (pre-redaction) text.
    injection_keywords = [
        "ignore instructions", "system prompt", "override rules",
        "jailbreak", "developer mode", "ignore prior instructions"
    ]
    is_injection = any(kw in user_text.lower() for kw in injection_keywords)

    # --- Layer 3: Domain Safety Rules ---
    # Blocks two categories of harmful requests:
    #   a) Attempts to acquire prescription drugs outside medical supervision
    #   b) Self-harm or suicide-related queries — redirected to crisis support
    blocked_keywords = [
        "prescription drugs without doctor", "buy illegal",
        "self-harm", "suicide", "kill myself"
    ]
    is_blocked = any(kw in user_text.lower() for kw in blocked_keywords)

    if is_injection:
        log_security_event(
            severity="CRITICAL",
            message="Prompt injection attempt detected.",
            user_input=user_text
        )
        # Route to violation handler; orchestrator is bypassed entirely.
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
        # Provide a supportive crisis message rather than a bare refusal.
        return Event(
            output="Safety warning: If you are experiencing a medical emergency or crisis, please seek immediate help. CareCompanion cannot process self-harm queries or unauthorized medication transactions.",
            route="violation",
            state={"security_status": "safety_violation"}
        )

    # All checks passed — log INFO and forward the cleaned text downstream.
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
    """Terminal node for blocked requests.

    Yields the pre-composed violation or crisis message from security_checkpoint
    as a model-role content event so the ADK playground renders it in the chat UI.
    No LLM is involved — the response is deterministic and immediate.
    """
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=node_input)]))
    yield Event(output=node_input)


# -----------------------------------------------------------------------------
# Model Initialization
# -----------------------------------------------------------------------------

# Gemini model shared across all LlmAgent instances.
# retry_options: automatically retries transient API errors up to 3 times,
# reducing the need for manual error handling in agent logic.
model = Gemini(
    model=config.model,
    retry_options=types.HttpRetryOptions(attempts=3),
)


# -----------------------------------------------------------------------------
# MCP Server Toolset
# -----------------------------------------------------------------------------

# Resolve the absolute path to mcp_server.py so the subprocess launch works
# regardless of the working directory from which the ADK server is started.
current_dir = os.path.dirname(os.path.abspath(__file__))
mcp_server_path = os.path.join(current_dir, "mcp_server.py")

# McpToolset launches mcp_server.py as a subprocess over stdio on first tool call.
# stdio transport avoids requiring a separate HTTP server — the MCP process lifecycle
# is managed automatically by the ADK runtime and torn down when the session ends.
mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", mcp_server_path],
        ),
    ),
)


# -----------------------------------------------------------------------------
# Sub-agents
# -----------------------------------------------------------------------------

# Sub-agents must use mode="chat" (the default) because they are invoked via
# AgentTool. ADK requires tool-wrapped agents to maintain conversational state
# across the tool call; "single_turn" mode is reserved for root graph nodes.

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
    # mcp_toolset gives this agent access to get_medication_info from the MCP server.
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
    # mcp_toolset gives this agent access to search_clinical_trials and get_trial_eligibility.
    tools=[mcp_toolset],
    mode="chat",
)


# -----------------------------------------------------------------------------
# Orchestrator (root graph node)
# -----------------------------------------------------------------------------

# The orchestrator is the root node of the workflow graph, so it must use
# mode="single_turn". It does not answer health questions directly — its sole
# responsibility is routing to the right specialist and synthesizing their output.
orchestrator = LlmAgent(
    name="orchestrator",
    model=model,
    instruction="""You are the CareCompanion Lead Concierge.
Coordinate the user's request. You have two specialized assistants available as tools:
- symptom_analyzer: Use this to check and analyze health symptoms.
- clinical_trial_advisor: Use this to search clinical trials and medical studies.

Route the request to the appropriate assistant, synthesize their response, and present a helpful, professional concierge summary back to the user.
""",
    # AgentTool wraps each sub-agent so the orchestrator can invoke them as tools.
    # This is the ADK multi-agent delegation pattern: the orchestrator decides WHAT
    # to ask; the sub-agents decide HOW to answer using their specialized tools.
    tools=[AgentTool(symptom_analyzer), AgentTool(clinical_trial_advisor)],
    mode="single_turn",
)


# -----------------------------------------------------------------------------
# HITL Consent Node
# -----------------------------------------------------------------------------

@node(rerun_on_resume=True)
def hitl_consent(ctx: Context, node_input: Any):
    """Human-in-the-loop gate: pauses execution and requests explicit user consent
    before surfacing clinical trial or symptom analysis results.

    Behavior:
      - First call (no resume inputs): detects health-sensitive content and yields
        a RequestInput interrupt, pausing the workflow.
      - Second call (after user replies): ADK resumes this node with rerun_on_resume=True,
        meaning the entire function re-executes with ctx.resume_inputs populated.
        If the user replied 'yes', the result is forwarded. Otherwise, a neutral
        decline message is shown — no diagnostic detail is revealed.

    The 'user_consent' state key prevents re-prompting if the node is visited
    again in the same session (e.g., after an error retry).
    """
    orchestrator_text = ""
    if isinstance(node_input, types.Content) and node_input.parts:
        orchestrator_text = "".join([part.text for part in node_input.parts if part.text])
    else:
        orchestrator_text = str(node_input)

    # Skip consent prompt if already approved in this session.
    if ctx.state.get("user_consent") == "approved":
        yield Event(output=orchestrator_text)
        return

    lower_text = orchestrator_text.lower()

    # Only trigger consent for health-sensitive outputs (trials or symptoms).
    # General informational responses flow through without interruption.
    if "trial" in lower_text or "symptom" in lower_text:
        if not ctx.resume_inputs or "consent" not in ctx.resume_inputs:
            # First pass: pause and ask for consent.
            yield RequestInput(
                interrupt_id="consent",
                message="CareCompanion is an AI assistant, not a medical professional. Before displaying matching trials or symptom analysis, please confirm you consent to the storage of this query for search and verification. Do you consent? (Type 'yes' or 'no')"
            )
            return

        # Second pass (resumed): read the user's reply.
        user_reply = ctx.resume_inputs.get("consent", "").strip().lower()
        if user_reply == "yes":
            yield Event(output=orchestrator_text, state={"user_consent": "approved"})
        else:
            # Decline path: do not surface any health detail.
            yield Event(output="Consent declined. CareCompanion cannot display clinical trial or symptom results without consent.")
    else:
        # Non-sensitive output — pass through without interruption.
        yield Event(output=orchestrator_text)


# -----------------------------------------------------------------------------
# Final Response Node
# -----------------------------------------------------------------------------

def final_response(node_input: str):
    """Terminal display node. Wraps the approved result in a model-role content
    event so the ADK playground renders it correctly in the chat UI.
    """
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=node_input)]))
    yield Event(output=node_input)


# -----------------------------------------------------------------------------
# Workflow Definition
# -----------------------------------------------------------------------------

# The Workflow graph wires all nodes together with named conditional routes.
# Edge tuple format: (source_node, target_node) or (source_node, {"route": target_node})
#
# Key design: security_checkpoint returns route="violation" or route="clean".
# ADK uses these route names to select the correct outgoing edge — the orchestrator
# is structurally unreachable when a violation is detected.
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

# App is the ADK entry point. root_agent accepts a Workflow or a single LlmAgent.
# The 'app' name must match the directory name for `adk web app` discovery.
app = App(
    root_agent=workflow,
    name="app",
)
