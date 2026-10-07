"""Curated discovery links, NOT canned answers or verified medical facts.

Known topic/goal pairs can fetch patient education before a literature search.
Every answer still requires fetched text, relevance, citations and safety review.
"""
import re
from ..dialogue_policy import normalize_medical_terms

SOURCES = {
    ('atrial_fibrillation', 'diagnosis'): 'https://www.nhlbi.nih.gov/health/atrial-fibrillation/diagnosis',
    ('arrhythmia', 'symptoms'): 'https://www.nhlbi.nih.gov/health/arrhythmias/symptoms',
    ('arrhythmia', 'diagnosis'): 'https://www.nhlbi.nih.gov/health/arrhythmias/diagnosis',
}


def education_links(query):
    query = normalize_medical_terms(query)
    topic = ('atrial_fibrillation' if re.search(r'房颤|心房颤动|atrial fibrillation|\bAFib\b', query, re.I)
             else 'arrhythmia' if re.search(r'心律不齐|心律失常|\barrhythmias?\b', query, re.I) else '')
    goals = []
    if re.search(r'检查|诊断|diagnos|\btests?\b', query, re.I):
        goals.append('diagnosis')
    if re.search(r'症状|表现|symptoms?|\bsigns?\b', query, re.I):
        goals.append('symptoms')
    return [{'url': SOURCES[(topic, goal)], 'goal': goal} for goal in goals if (topic, goal) in SOURCES]


def education_metadata(url):
    for (topic, goal), source in SOURCES.items():
        if url.rstrip('/') == source.rstrip('/'):
            return {'source_category': 'patient_education', 'source_topic': topic, 'source_goal': goal,
                    'publisher': 'NIH / NHLBI', 'evidence_extent': 'patient_education_body'}
    return {}
