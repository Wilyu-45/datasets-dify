"""验证两个修复。"""
import sys
sys.path.insert(0, r"d:\programmtools\tools\ragsystem\backend")

# 测试 1: 中文数字序号 L1 识别
from app.services.chunker import RE_CN_NUMERAL_L1, _infer_block_level, _looks_like_l1_title, Block

test_cases = [
    ("一、门诊设置", True),
    ("二、病区设置", True),
    ("十、管理要求", True),
    ("（一）独立设置", False),
    ("1. 范围", False),
    ("一、下面是具体内容..." + "x" * 40, False),
]

print("=== 测试 RE_CN_NUMERAL_L1 ===")
for text, expected in test_cases:
    result = bool(RE_CN_NUMERAL_L1.match(text))
    status = "OK" if result == expected else "FAIL"
    print(f"  [{status}] '{text[:30]}' -> {result} (expected {expected})")

print("\n=== 测试 _looks_like_l1_title ===")
for text, expected in test_cases:
    result = _looks_like_l1_title(text)
    status = "OK" if result == expected else "FAIL"
    print(f"  [{status}] '{text[:30]}' -> {result} (expected {expected})")

print("\n=== 测试 _infer_block_level ===")
for text, expected_l1 in test_cases:
    block = Block(page_num=1, block_type="title", level=None, text=text)
    result = _infer_block_level(block)
    expected = 1 if expected_l1 else None
    status = "OK" if result == expected else "FAIL"
    print(f"  [{status}] '{text[:30]}' -> level={result} (expected {expected})")

# 测试 2: 长路径支持
from app.services.mineru_client import _long_path
from pathlib import Path

print("\n=== 测试 _long_path ===")
short_path = Path(r"D:\data\test.md")
long_str = r"D:\programmtools\tools\ragsystem\data\parsed\sheaidsaapic_practice_recommendation_strategies_to_prevent_healthcareassociated_infections_through_hand_hygiene_2022_update\vlm\sheaidsaapic_practice_recommendation_strategies_to_prevent_healthcareassociated_infections_through_hand_hygiene_2022_update_text.md"
long_path = Path(long_str)

result_short = _long_path(short_path)
result_long = _long_path(long_path)
prefix = "\\\\?\\"

print(f"  Short path ({len(str(short_path))} chars): {result_short}")
print(f"  Long path ({len(str(long_path))} chars): starts with prefix: {result_long.startswith(prefix)}")
print(f"  Long path result: {result_long[:60]}...")
