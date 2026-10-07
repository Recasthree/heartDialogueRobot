"""Small, deterministic PII redaction helpers for local research fixtures."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# Require a surname and complete name boundary instead of arbitrary Chinese words.
SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林钟徐邱骆高夏蔡田樊胡凌霍虞万支柯卢莫房裘缪解应宗丁宣邓郁单杭洪包诸左石崔龚程邢裴陆荣翁荀羊甄曲封储靳段富巫乌焦巴弓牧山谷车侯宓蓬全班仰秋仲伊宫宁仇栾暴甘厉戎祖武符刘景詹龙叶幸司黎薄印宿白怀蒲台鄂索赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边燕冀郏浦尚农温别庄晏柴瞿阎连习容向古易慎戈廖庾终居衡步都耿满弘匡国文寇广禄阙东欧沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾沙养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公闫"
MEDICAL_WORDS = re.compile(r"胸痛|胸闷|大汗|房颤|高血压|低血压|心衰|心悸|头晕|患者|医生|护士|呼吸|水肿|意识|心脏|冠心病|血糖|检查|病史|血脂|低血糖|胃痛|头痛")


SURNAMES += "付覃"

PII_PATTERNS = [
    ("name_at_start", re.compile(r"^(?!患者|我叫|我是|医生|护士)[\u4e00-龥]{2,4}(?=[，,\s])"), "[姓名]"),
    ("name_in_table", re.compile(r"(?<=\|\s)(?!(?:姓名|电话|身份证号)\s\|)[\u4e00-龥]{2,4}(?=\s\|)"), "[姓名]"),
    ("name_json", re.compile(r'("患者姓名"\s*:\s*")([^"]+)(")'), "[姓名]"),
    ("name_between_commas", re.compile(r"(?<=[，,])(?!您好|你好|谢谢|医生|患者|我是|我叫|男性|女性)[\u4e00-龥]{2,4}(?=[，,])"), "[姓名]"),
    ("address_standalone", re.compile(r"(?:^|(?<=[，,。；;\s]))(?!地址|住址)[\u4e00-龥]{2,24}(?:路|街|道)\d+[号號](?:\d+[栋棟楼樓]?)?(?:\d+室)?"), "[地址]"),
    ("id_card", re.compile(r"((?:身份证号|身份证号码|身份号码|身份证|证件号|身奋证号)(?:\s*[:：]?\s*|\s*是\s*))(\d{17,18}(?:[0-9Xx])?)"), "[身份证号]"),
    ("id_card_table", re.compile(r"(?<=\|\s)\d{17,18}(?:[0-9Xx])?(?=\s\|)"), "[身份证号]"),
    ("phone", re.compile(r"(?<![\d０-９])(?:1[3-9]\d{9}|[１-９][０-９]{10})(?![\d０-９])"), "[手机号]"),
    ("medical_insurance_number", re.compile(r"(?i)(医保卡号)\s*[:：]\s*\d{6,}"), "医保卡号：[医保卡号]"),
    ("inpatient_number", re.compile(r"(?i)(?<![A-Z0-9])(?:ZY|MZ|BA|住院|病案|MR)[-：:#\s]*[A-Z0-9-]{5,}(?![A-Z0-9])"), "[住院号]"),
    ("address", re.compile(r"(?i)((?:家庭|联系|居住)?地址|住址)(?:\s*[:：]\s*|(?=[\u4e00-\u9fff]))[^，。;；\n\[]+"), "地址：[地址]"),
    ("name_after_patient", re.compile(r"(?<=患者)(?!身份证|身份号码|医保|电话|手机号|住院|病案|姓名)[\u4e00-龥]{2,4}"), "[姓名]"),
    ("name_after_intro", re.compile(r"(?<=我叫)[\u4e00-龥]{2,4}"), "[姓名]"),
    ("name_after_self_intro", re.compile(r"(?<=我是)[\u4e00-龥]{2,4}"), "[姓名]"),
    ("email", re.compile(r"(?i)(?<![A-Z0-9._%+-])[\u4e00-\u9fffA-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9._%+-])"), "[邮箱]"),
]


def redact_text(text: str) -> Tuple[str, List[Dict[str, str]]]:
    """Return redacted text and deterministic entity spans for tests/logging."""
    redacted = str(text or "")
    entities: List[Dict[str, str]] = []
    for entity_type, pattern, replacement in PII_PATTERNS:
        def permitted(match):
            if not entity_type.startswith('name_') or entity_type == 'name_json':
                return True
            value = match.group(0)
            end = match.end()
            bounded = end == len(redacted) or not ('\u4e00' <= redacted[end] <= '\u9fff')
            return bool(value and value[0] in SURNAMES and bounded and not MEDICAL_WORDS.search(value))
        matches = [match for match in pattern.finditer(redacted) if permitted(match)]
        for match in matches:
            if entity_type in {"name_json", "id_card"} and match.lastindex and match.lastindex >= 2:
                value = match.group(2)
            else:
                value = match.group(0)
            entities.append({"type": entity_type, "value": value})
        if entity_type == "id_card":
            redacted = pattern.sub(lambda match: "%s[身份证号]" % match.group(1), redacted)
        elif entity_type == "name_json":
            redacted = pattern.sub(lambda match: "%s[姓名]%s" % (match.group(1), match.group(3)), redacted)
        elif entity_type == "address":
            redacted = pattern.sub(lambda match: "%s%s[地址]" % (match.group(1), "：" if ":" in match.group(0) or "：" in match.group(0) else ""), redacted)
        else:
            redacted = pattern.sub(lambda match: replacement if permitted(match) else match.group(0), redacted)
    return redacted, entities


def redact_payload(value: Any, context: str = '') -> Any:
    """Recursively redact strings in a JSON-like payload."""
    if isinstance(value, str):
        return redact_text(value)[0]
    if isinstance(value, list):
        return [redact_payload(item, context) for item in value]
    if isinstance(value, dict):
        sensitive = {'name', 'patient_name', 'full_name', '姓名', '患者姓名', 'phone', 'mobile', 'email', 'address', '身份证号'}
        if context in {'medications', 'lab_results', 'drugs', 'drug', 'symptom', 'disease'}:
            sensitive.discard('name')
        return {str(key): '[个人信息]' if str(key).lower() in sensitive and item else redact_payload(item, str(key))
                for key, item in value.items()}
    return value
