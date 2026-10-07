"""Request scope and user-facing wording; no medical facts are inferred here."""
import re

MEDICATION_SELECTION = re.compile(
    r'(?:要|应该|该|可以|能)?(?:吃|用|服用)(?:什么|哪种|哪些)药|推荐.{0,8}药|'
    r'(?:吃多少|多少毫克|吃几次|吃几片|加倍吃|停药|停用|加量|减量|换药|补服)|'
    r'what (?:medicine|medication|drug).{0,15}(?:take|use)|how (?:should|do) i take', re.I)
INFORMATION_GOAL = re.compile(r'症状|表现|检查|病因|原因|symptoms?|diagnostic tests|causes?', re.I)
MEDICATION_BOUNDARY = '关于用药：不能仅凭这段描述为你选药或确定剂量，请让医生结合检查结果判断。'


def normalize_medical_terms(query):
    # Retrieval alias only, not a diagnosis or a rewrite of stored user facts.
    return query.replace('心率不齐', '心律不齐')


def request_scope(query, unsafe=False):
    personal_medication = bool(MEDICATION_SELECTION.search(query)) or unsafe
    if not personal_medication:
        return {'answer_query': normalize_medical_terms(query), 'medication_boundary': False}
    parts = []
    for clause in re.split(r'[，,。；;？！?!\n]|(?:另外|顺便|以及|并且)', query):
        match = MEDICATION_SELECTION.search(clause)
        safe = clause[:match.start()] if match else clause
        # A clear, independent general-information goal is required. Do not
        # turn a dosage/interaction question into an invented symptoms task.
        if INFORMATION_GOAL.search(safe) and len(safe.strip(' 和及还要')) >= 4:
            parts.append(safe.strip(' 和及还要'))
    return {'answer_query': normalize_medical_terms('；'.join(parts) if parts else query),
            'medication_boundary': bool(parts)}


def independent_information_query(query, has_subject):
    return bool(has_subject and INFORMATION_GOAL.search(query)
                and not re.search(r'它|这种|那个|上述|上次|刚才|之前|我的|我该|我有|我现在|报告|病历', query))


def unsafe_answer(text):
    # Keep the strict dosage boundary. Negation handling below only affects
    # action verbs, not numeric drug instructions or the model safety review.
    if re.search(r'\d+(?:\.\d+)?\s*(?:mg|毫克|片/天|片每天)', text, re.I):
        return True
    for clause in re.split(r'[，,。；;！!\n]|但是|不过|然而|而是|但', text):
        for action in re.finditer(r'停药|加量|加倍|确诊为', clause):
            prefix = clause[:action.start()]
            if not re.search(r'(?:你|您|建议|应当|应|请).{0,15}$', prefix):
                continue
            negated = re.search(r'(?:不能|不可|不应|不要|勿|不得|避免|禁止|切勿|不建议|并非)[^，。；;\n]{0,10}$', prefix)
            double_negative = re.search(r'(?:并非|不是|不要|不能说).{0,3}(?:不|勿|禁止)', prefix)
            if not negated or double_negative:
                return True
    return False


def patient_wording(text):
    """Translate metadata jargon, retaining the actual uncertainty/limitation."""
    text = re.sub(r'`?medical_truth_verified`?\s*[=:：]\s*`?false`?', '尚未经过医学核验', text, flags=re.I)
    replacements = {'review_status':'来源审核情况', 'medical_truth_verified':'医学核验情况',
                    'evidence_status':'来源情况', 'support_mode':'回答依据类型',
                    'pending':'尚未完成专业审核', 'unverified':'尚未核验', 'approved':'已完成来源审核'}
    for key, value in replacements.items():
        text = re.sub(r'(?<![A-Za-z_])`?'+key+r'`?(?![A-Za-z_])', value, text, flags=re.I)
    return text
