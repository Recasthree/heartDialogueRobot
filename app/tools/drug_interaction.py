from ..providers import UnconfiguredMedicalToolProvider


class DrugInteractionTool(UnconfiguredMedicalToolProvider):
    def __init__(self, path=None):
        from pathlib import Path
        from ..retrieval.corpus import load_jsonl
        self.rows = list(load_jsonl(Path(path))) if path else []

    async def query(self, drug_a: str, drug_b: str):
        if self.rows:
            for row in self.rows:
                pair = {str(row.get('drug_a', '')).lower(), str(row.get('drug_b', '')).lower()}
                if pair == {drug_a.lower(), drug_b.lower()}:
                    return {'status': 'found', 'configured': True, 'record': row}
            return {'status': 'no_record', 'configured': True, 'advice': '查无记录不代表不存在相互作用。'}
        return {
            "status": "unavailable",
            "configured": False,
            "drug_a": drug_a,
            "drug_b": drug_b,
            "severity": "unknown",
            "advice": "当前未接入药物相互作用数据源，不提供判断。",
        }

