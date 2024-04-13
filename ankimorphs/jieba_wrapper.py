from __future__ import annotations

import importlib
import importlib.util
import sys
from types import ModuleType

from ankimorphs.morpheme import Morpheme

posseg: ModuleType | None = None

successful_startup: bool = False

################################################################################
# This section about cjk_ideographs is based on zhon/hanzi.py in:
# https://github.com/tsroten/zhon
################################################################################

#: Character code ranges for pertinent CJK ideograph Unicode blocks.
# cjk_ideograph_unicode_ranges = [
#     r"\u3007",  # Ideographic number zero
#     r"[\u4E00-\u9FFF]",  # CJK Unified Ideographs
#     r"[\u3400-\u4DBF]",  # CJK Unified Ideographs Extension A
#     "\uF900-\uFAFF",  # CJK Compatibility Ideographs
# ]

cjk_ideograph_unicode_ranges = [
    (0x3007, 0x3007),
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0xF900, 0xFAFF),
]

if sys.maxunicode > 0xFFFF:
    # cjk_ideograph_unicode_ranges += [
    #     r"[\U00020000-\U0002A6DF]",  # CJK Unified Ideographs Extension B
    #     r"[\U0002A700-\U0002B73F]",  # CJK Unified Ideographs Extension C
    #     r"[\U0002B740-\U0002B81F]",  # CJK Unified Ideographs Extension D
    #     r"[\U0002F800-\U0002FA1F]",  # CJK Compatibility Ideographs Supplement
    # ]
    cjk_ideograph_unicode_ranges += [
        (0x20000, 0x2A6DF),
        (0x2A700, 0x2B73F),
        (0x2B740, 0x2B81F),
        (0x2F800, 0x2FA1F),
    ]


################################################################################


def import_jieba() -> None:
    global posseg, successful_startup

    if importlib.util.find_spec("1857311956"):
        posseg = importlib.import_module("1857311956.jieba.posseg")
    elif importlib.util.find_spec("ankimorphs_chinese_jieba"):
        posseg = importlib.import_module("ankimorphs_chinese_jieba.jieba.posseg")
    else:
        return

    successful_startup = True


def get_morphemes_jieba(expression: str) -> list[Morpheme]:
    assert posseg is not None

    expression_morphs: list[Morpheme] = []

    for jieba_segment_pair in posseg.cut(expression):
        # posseg.Pair:
        #   Pair.word
        #   Pair.flag

        print(f"jieba_segment_pair.word: {jieba_segment_pair.word}")

        if not text_contains_only_cjk_ranges(_text=jieba_segment_pair.word):
            print("contains non-cjk-ideographs, invalid")
            continue

        print("valid cjk-ideographs")

        # chinese does not have inflections, so we use the lemma for both
        _morph = Morpheme(
            lemma=jieba_segment_pair.word, inflection=jieba_segment_pair.word
        )
        expression_morphs.append(_morph)

    return expression_morphs


def char_found_in_cjk_ranges(_char: str) -> bool:
    for start, end in cjk_ideograph_unicode_ranges:
        # print(f"start: {start}, end: {end}")
        if start <= ord(_char) <= end:
            return True
    return False


def text_contains_only_cjk_ranges(_text: str) -> bool:
    for char in _text:
        if not char_found_in_cjk_ranges(char):
            return False
    return True
