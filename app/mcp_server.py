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

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("care-companion-mcp")

# Mock data for clinical trials
CLINICAL_TRIALS_DB = [
    {
        "nct_id": "NCT05938472",
        "title": "A Study of Novel Insulin Delivery Systems in Type 1 Diabetes",
        "condition": "Type 1 Diabetes",
        "phase": "Phase 2",
        "location": "Boston, MA",
        "status": "RECRUITING",
        "eligibility": "Age 18-65, diagnosed with Type 1 Diabetes for at least 1 year. HbA1c between 7.0% and 9.0%. Exclusion: Pregnancy, renal impairment.",
        "description": "This study evaluates the safety and efficacy of a new automated insulin delivery system."
    },
    {
        "nct_id": "NCT04102938",
        "title": "Lifestyle Modification and Dietary Intervention in Hypertension",
        "condition": "Hypertension",
        "phase": "Phase 3",
        "location": "San Francisco, CA",
        "status": "RECRUITING",
        "eligibility": "Age 20-75, diagnosed with Stage 1 or Stage 2 Hypertension. Not currently taking more than one anti-hypertensive medication. Exclusion: History of cardiovascular events.",
        "description": "Evaluating the long-term impact of a DASH-based diet combined with guided aerobic exercise."
    },
    {
        "nct_id": "NCT08830192",
        "title": "Immunotherapy Combination in Advanced Non-Small Cell Lung Cancer",
        "condition": "Lung Cancer",
        "phase": "Phase 3",
        "location": "Chicago, IL",
        "status": "RECRUITING",
        "eligibility": "Age 18+, histologically confirmed Stage IV non-small cell lung cancer. ECOG performance status 0 or 1. Exclusion: Active autoimmune disease.",
        "description": "A randomized trial comparing a novel immunotherapy combination against standard chemotherapy."
    }
]

# Mock data for medications
MEDICATIONS_DB = {
    "metformin": {
        "drug_name": "Metformin",
        "class": "Biguanide Antidiabetic",
        "standard_dosage": "500mg, 850mg, or 1000mg tablets, usually taken with meals once or twice daily.",
        "precautions": "Take with meals to reduce gastrointestinal side effects. Monitor kidney function regularly.",
        "contraindications": "Severe renal impairment (eGFR < 30 mL/min), acute or chronic metabolic acidosis.",
        "interactions": "Contrast dyes (hold Metformin before/after imaging studies), cimetidine, dolutegravir."
    },
    "lisinopril": {
        "drug_name": "Lisinopril",
        "class": "ACE Inhibitor (Antihypertensive)",
        "standard_dosage": "5mg to 40mg once daily.",
        "precautions": "Monitor blood pressure regularly. Avoid potassium supplements or salt substitutes containing potassium without consulting a doctor.",
        "contraindications": "History of angioedema related to previous ACE inhibitor treatment, pregnancy.",
        "interactions": "NSAIDs (may decrease antihypertensive effect and increase risk of renal impairment), lithium, potassium-sparing diuretics."
    },
    "atorvastatin": {
        "drug_name": "Atorvastatin",
        "class": "HMG-CoA Reductase Inhibitor (Statin)",
        "standard_dosage": "10mg to 80mg once daily, at any time of day.",
        "precautions": "Avoid large quantities of grapefruit juice. Monitor liver enzymes and report unexplained muscle pain immediately.",
        "contraindications": "Active liver disease, pregnancy, breastfeeding.",
        "interactions": "Clarithromycin, itraconazole, cyclosporine (increase risk of muscle toxicity/myopathy)."
    }
}

@mcp.tool()
def search_clinical_trials(condition: str, location: str = None) -> list:
    """Search for clinical trials matching a medical condition and optional location.

    Args:
        condition: The medical condition (e.g., 'Diabetes', 'Hypertension', 'Lung Cancer').
        location: Optional city or state to filter trials (e.g., 'Boston, MA', 'San Francisco, CA').

    Returns:
        A list of matching trial dicts with details.
    """
    results = []
    condition_lower = condition.lower()
    for trial in CLINICAL_TRIALS_DB:
        if condition_lower in trial["condition"].lower():
            if location:
                if location.lower() in trial["location"].lower():
                    results.append(trial)
            else:
                results.append(trial)
    return results

@mcp.tool()
def get_trial_eligibility(nct_id: str) -> dict:
    """Retrieve detailed eligibility criteria and info for a trial by NCT ID.

    Args:
        nct_id: The unique National Clinical Trial (NCT) ID (e.g., 'NCT05938472').

    Returns:
        A dictionary containing eligibility criteria and details.
    """
    for trial in CLINICAL_TRIALS_DB:
        if trial["nct_id"].upper() == nct_id.upper():
            return {
                "nct_id": trial["nct_id"],
                "title": trial["title"],
                "eligibility": trial["eligibility"],
                "status": trial["status"],
                "description": trial["description"]
            }
    return {"error": f"Clinical trial with ID {nct_id} not found."}

@mcp.tool()
def get_medication_info(drug_name: str) -> dict:
    """Retrieve details, dosages, interactions, and precautions for a drug.

    Args:
        drug_name: The generic drug name (e.g., 'Metformin', 'Lisinopril', 'Atorvastatin').

    Returns:
        A dictionary containing drug information and guidelines.
    """
    name_lower = drug_name.lower().strip()
    if name_lower in MEDICATIONS_DB:
        return MEDICATIONS_DB[name_lower]
    return {"error": f"Medication information for '{drug_name}' is not in the database. Please verify spelling."}

if __name__ == "__main__":
    mcp.run()
