"""员工细分需求必须拒绝，不能用总人数代答。"""

import re

from app.qa.facts import metric_in


def unsupported_employee_request(query: str) -> bool:
    if "员工" not in query:
        return False
    if re.search(
        r"(?:员工数量|员工人数|在职员工)(?:中|里|当中)|其中|剔除|不含派遣"
        r"|只算|仅限|会.*(?:外语|英语|语言)|掌握|精通|身高|养猫|养狗|博士|硕士"
        r"|学历|研发人员|生产人员", query,
    ):
        return True
    return metric_in(query) is None
