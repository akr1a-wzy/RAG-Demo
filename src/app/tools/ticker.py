import re

CODE_TO_CANONICAL_NAME = {
    "000333": "美的集团",
    "000568": "泸州老窖",
    "000651": "格力电器",
    "000858": "五粮液",
    "002415": "海康威视",
    "002594": "比亚迪",
    "300750": "宁德时代",
    "600019": "宝钢股份",
    "600028": "中国石化",
    "600030": "中信证券",
    "600036": "招商银行",
    "600050": "中国联通",
    "600276": "恒瑞医药",
    "600519": "贵州茅台",
    "600887": "伊利股份",
    "600900": "长江电力",
    "601012": "隆基绿能",
    "601088": "中国神华",
    "601318": "中国平安",
    "601857": "中国石油",
}

# 只收录在当前20家公司内无歧义的简称。不收录裸“平安”、“中信”等词。
ALIAS_TO_CODE = {
    **{name: code for code, name in CODE_TO_CANONICAL_NAME.items()},
    "美的": "000333",
    "格力": "000651",
    "海康": "002415",
    "宁德": "300750",
    "宝钢": "600019",
    "中石化": "600028",
    "招行": "600036",
    "联通": "600050",
    "恒瑞": "600276",
    "茅台": "600519",
    "伊利": "600887",
    "长电": "600900",
    "隆基": "601012",
    "神华": "601088",
    "中国平安保险": "601318",
    "中石油": "601857",
}

# 临时兼容旧导入；新代码应使用上面两张映射。
STOCK_NAME_TO_CODE = ALIAS_TO_CODE


def normalize_ticker(value: str) -> str:
    """把公司名和常见证券代码格式统一为 6 位 A 股代码。"""

    candidate = value.strip()
    if candidate in ALIAS_TO_CODE:
        return ALIAS_TO_CODE[candidate]

    compact = re.sub(r"\s+", "", candidate).upper()
    patterns = (
        r"^(?:SH|SZ|BJ)?(\d{6})$",
        r"^(\d{6})\.(?:SH|SZ|BJ)$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, compact)
        if match:
            return match.group(1)

    raise ValueError(f"无法识别股票代码或公司名: {value!r}")


def market_prefix(code: str) -> str:
    """返回腾讯行情接口使用的市场前缀。"""

    normalized = normalize_ticker(code)
    if normalized.startswith(("6", "9")):
        return "sh"
    if normalized.startswith(("8", "4")):
        return "bj"
    return "sz"
