from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple

from extractors import get_function_source
from symbols import _strip_block_comments, _strip_inline_comments

# ------------------- DATATYPE TEST VALUES -------------------
# --- Return type resolution & Cantata CHECK macro selection ---

_C_STD_INT_TYPES = {
    "char", "signed char", "unsigned char",
    "short", "short int", "signed short", "signed short int", "unsigned short", "unsigned short int",
    "int", "signed", "signed int", "unsigned", "unsigned int",
    "long", "long int", "signed long", "signed long int", "unsigned long", "unsigned long int",
    "long long", "long long int", "signed long long", "signed long long int", "unsigned long long", "unsigned long long int",
}

_NO_RETURN_CHECK = "__NO_RETURN_CHECK__"

def _normalize_c_type(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\s+", " ", t)
    # remove common qualifiers and storage class
    t = re.sub(r"\b(const|volatile|static|inline|extern|register|restrict)\b", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def _is_pointer_type(t: str) -> bool:
    return "*" in (t or "")

def _strip_ptr_and_parens(t: str) -> str:
    t = _normalize_c_type(t)
    t = t.replace("*", " ")
    t = t.replace("(", " ").replace(")", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t

def _parse_includes_from_c_file(c_file_path: str) -> List[str]:
    """
    Return include basenames in appearance order: ["Os.h", "IOconfig.h", ...]
    Only returns the filename part, not directories.
    """
    try:
        txt = Path(c_file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    incs = []
    for m in re.finditer(r'^[ \t]*#\s*include\s*[<"]([^">]+)[">]', txt, flags=re.MULTILINE):
        inc_path = m.group(1).strip()
        incs.append(os.path.basename(inc_path))
    return incs

def _extract_typedef_map_from_header_text(text: str) -> Dict[str, str]:
    """
    Very lightweight typedef extractor:
      typedef signed short int S16;
      typedef signed char SMODE;
      typedef UINT16 Foo;
    Returns { "S16": "signed short int", "SMODE": "signed char", ... }
    """
    m: Dict[str, str] = {}

    # remove comments first
    text = _strip_block_comments(text)
    text = _strip_inline_comments(text)

    # typedef <type> <name>;
    # (keeps it simple, but works for typical embedded typedefs)
    td_re = re.compile(r"\btypedef\s+([^;]+?)\s+([A-Za-z_]\w*)\s*;", re.MULTILINE)
    for mm in td_re.finditer(text):
        src = _normalize_c_type(mm.group(1))
        name = mm.group(2).strip()
        if name:
            m[name] = src
    return m

def _build_typedef_db(header_index: Dict[str, str], include_basenames: List[str]) -> Dict[str, str]:
    typedefs: Dict[str, str] = {}

    # pass 1: included headers first
    for base in include_basenames:
        hp = (header_index or {}).get(base)
        if not hp or not os.path.isfile(hp):
            continue
        try:
            text = Path(hp).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        typedefs.update(_extract_typedef_map_from_header_text(text))

    # pass 2 (fallback): scan all headers in header_index
    for base, hp in (header_index or {}).items():
        if base in typedefs:  # not necessary, but small optimization
            pass
        if not hp or not os.path.isfile(hp):
            continue
        try:
            text = Path(hp).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        typedefs.update(_extract_typedef_map_from_header_text(text))

    return typedefs

def _resolve_typedef_chain(type_name: str, typedef_db: Dict[str, str], max_hops: int = 20) -> str:
    """
    Resolve: S16 -> signed short int -> short int -> ...
    Returns normalized string.
    """
    t = _strip_ptr_and_parens(type_name)
    for _ in range(max_hops):
        nxt = typedef_db.get(t)
        if not nxt:
            break
        t = _strip_ptr_and_parens(nxt)
    return t

def _cantata_check_macro_for_resolved_type(resolved: str) -> Optional[str]:
    """
    Map resolved C type to Cantata CHECK macro.
    Adjust these mappings if your Cantata installation uses different macro names.
    """
    r = _normalize_c_type(resolved).lower()

    # signed/unsigned char
    if r in ("char", "signed char"):
        return "CHECK_S_CHAR"
    if r == "unsigned char":
        return "CHECK_U_CHAR"

    # short
    if r in ("short", "short int", "signed short", "signed short int"):
        return "CHECK_S_INT"   # common Cantata style for 16-bit signed
    if r in ("unsigned short", "unsigned short int"):
        return "CHECK_U_INT"

    # int
    if r in ("int", "signed", "signed int"):
        return "CHECK_S_INT"
    if r in ("unsigned", "unsigned int"):
        return "CHECK_U_INT"

    # long
    if r in ("long", "long int", "signed long", "signed long int"):
        return "CHECK_S_INT"
    if r in ("unsigned long", "unsigned long int"):
        return "CHECK_U_INT"

    # long long
    if r in ("long long", "long long int", "signed long long", "signed long long int"):
        return "CHECK_S_INT"
    if r in ("unsigned long long", "unsigned long long int"):
        return "CHECK_U_INT"

    # float/double
    if r == "float":
        return "CHECK_FLOAT"
    if r == "double":
        return "CHECK_DOUBLE"

    return None

def _emit_return_check_line(ret_type: str, ret_check_macro: Optional[str], resolved_ret: str) -> str:
    if not ret_type or ret_type.strip().lower() == "void":
        return ""

    if ret_check_macro == _NO_RETURN_CHECK:
        return ""

    if ret_check_macro:
        return f"{ret_check_macro}(returnValue, expected_returnValue);"

    if resolved_ret:
        return f"/* TODO: No CHECK macro mapping for resolved return type: {resolved_ret} */"
    return f"/* TODO: No CHECK macro mapping for return type: {ret_type} */"


def get_typename_group(typename):
    """
    Map a type name to its value group for test value generation.
    Accepts common typedefs and aliases.
    """
    t = typename.strip().upper()
    if any(x in t for x in ['UINT8', 'U8', 'FLAG', 'MODE']):
        return 'U8'
    if any(x in t for x in ['UINT16', 'U16']):
        return 'U16'
    if any(x in t for x in ['UINT32', 'U32', 'TIME_MSEC', 'TIME_USEC']):
        return 'U32'
    if 'UINT64' in t:
        return 'U64'
    if any(x in t for x in ['SINT8', 'S8', 'SFLAG', 'SMODE']):
        return 'S8'
    if any(x in t for x in ['SINT16', 'S16']):
        return 'S16'
    if any(x in t for x in ['SINT32', 'S32']):
        return 'S32'
    if 'SINT64' in t:
        return 'S64'
    return None


def test_values_for_type(typename):
    """
    For a given type name, return [min, random, max] values as hex strings with U suffix.
    Signed types use two's complement for negatives.
    Unsigned types use 0, random, max.
    """
    import random
    groups = {
        'U8': (0x00, 0xFF, 2),
        'U16': (0x0000, 0xFFFF, 4),
        'U32': (0x00000000, 0xFFFFFFFF, 8),
        'U64': (0x0000000000000000, 0xFFFFFFFFFFFFFFFF, 16),
        'S8': (-128, 127, 2),
        'S16': (-32768, 32767, 4),
        'S32': (-2147483648, 2147483647, 8),
        'S64': (-9223372036854775808, 9223372036854775807, 16),
    }
    group = get_typename_group(typename)
    if not group or group not in groups:
        return None

    min_val, max_val, hex_width = groups[group]

    # Random value between min and max, not equal to min or max
    if group.startswith('U'):
        if max_val - min_val > 1:
            rand_val = random.randint(min_val + 1, max_val - 1)
        else:
            rand_val = min_val
        return [
            f"0x{min_val:0{hex_width}X}U",
            f"0x{rand_val:0{hex_width}X}U",
            f"0x{max_val:0{hex_width}X}U"
        ]
    else:
        # For signed, encode negatives as two's complement hex
        bits = hex_width * 4

        def to_hex(val):
            if val < 0:
                val = (1 << bits) + val
            return f"0x{val:0{hex_width}X}U"

        if max_val - min_val > 1:
            rand_val = random.randint(min_val + 1, max_val - 1)
        else:
            rand_val = 0
        return [
            to_hex(min_val),
            to_hex(rand_val),
            to_hex(max_val)
        ]


# ------------------- SMALL HELPERS USED BY GENERATOR -------------------


def special_assignment_value(expected_val: str) -> str:
    """
    If expected == 0 -> 0x1F
    If expected != 0 -> 0
    If expected == TRUE -> FALSE
    If expected == FALSE -> TRUE
    Handles hex/decimal/boolean.
    """
    s = expected_val.strip()
    if s.upper() == "TRUE":
        return "FALSE"
    if s.upper() == "FALSE":
        return "TRUE"
    m_hex = re.match(r'^0x([0-9A-Fa-f]+)U$', s)
    if m_hex:
        val = int(m_hex.group(1), 16)
        if val == 0:
            return "0x1FU"
        else:
            return "0U"
    m_dec = re.match(r'^([0-9]+)U$', s)
    if m_dec:
        val = int(m_dec.group(1))
        if val == 0:
            return "0x1FU"
        else:
            return "0U"
    m_hex0 = re.match(r'^0x([0-9A-Fa-f]+)$', s)
    if m_hex0 and int(m_hex0.group(1), 16) == 0:
        return "0x1FU"
    m_dec0 = re.match(r'^([0-9]+)$', s)
    if m_dec0 and int(m_dec0.group(1)) == 0:
        return "0x1FU"
    return "0U"


def make_cantata_header(func, c_file_path, purpose=None, sdd_link="", srd_link=""):
    name = func['name']
    ret_type = (func.get('return_type') or '').strip()
    args = func.get('args', [])
    module_file = os.path.basename(c_file_path) if c_file_path else ""
    args_list = ", ".join([p for _, p in args]) or "None"
    output = "-" if not ret_type or ret_type.lower() == "void" else ret_type

    return (
        "/**************************************************************************************************\n"
        "*\n"
        f"* Function: {name}\n"
        "*\n"
        f"* Purpose: To test the function \n"
        "*\n"
        "* Software Design Document Link:\n"
        "*\n"
        "* Software Requirement Link:\n"
        "*\n"
        f"* Input: {args_list}\n"
        "*\n"
        f"* Output: \n"
        "*\n"
        " *************************************************************************************************/"
    )

def generate_atest_file_header(source_c_filename: str, author: Optional[str] = None, generated_on: Optional[str] = None) -> str:
    """
    Generate the Cantata file-level prologue to be placed ONCE at the top of each output.
    - Filename: atest_<module>.c where module is the base of the .c file
    - Author: OS username
    - Generated on: current time (dd-Mon-YYYY HH:MM:SS)
    - Generated from: original .c filename
    """
    import getpass
    import datetime

    base = os.path.basename(source_c_filename or "")
    module = os.path.splitext(base)[0] if base else "module"
    atest_filename = f"atest_{module}.c"

    if author is None:
        author = getpass.getuser()
    if generated_on is None:
        generated_on = datetime.datetime.now().strftime("%d-%b-%Y %H:%M:%S")

    return (
        "/*****************************************************************************/\n"
        "/*                            Cantata Test Script                            */\n"
        "/*****************************************************************************/\n"
        "/*\n"
        f" *    Filename: {atest_filename}\n"
        f" *    Author: {author}\n"
        f" *    Generated on: {generated_on}\n"
        f" *    Generated from: {base}\n"
        " */\n"
    )

def generate_test_cases_section_header() -> str:
    return (
        "/*****************************************************************************/\n"
        "/* Test Cases                                                                */\n"
        "/*****************************************************************************/\n"
    )

def generate_test_control_block(module_name: str, test_numbers: List[int]) -> str:
    """
    Generate the Cantata 'Test Control' section.
    - module_name: base name of the .c file (e.g., "cluster_info_interface")
    - test_numbers: list of generated test numbers (e.g., [204,205,...])
    """
    cov_name = f"atest_{module_name}.cov"

    lines = [
        "/*****************************************************************************/",
        "/* Test Control                                                              */",
        "/*****************************************************************************/",
        "/* run_tests() contains calls to the individual test cases, you can turn test*/",
        "/* cases off by adding comments*/",
        "void run_tests()",
        "{",
    ]

    for n in test_numbers:
        lines.append(f"    test_{n}(1);")

    lines += [
        "",
        '    rule_set("*", "*");',
        f'    EXPORT_COVERAGE("{cov_name}", cppca_export_replace);',
        "}\n",
        "",
    ]
    return "\n".join(lines)

def _is_struct_or_union_type_name_in_text(type_name: str, text: str) -> bool:
    """
    Detect:
      typedef struct { ... } TypeName;
      typedef union  { ... } TypeName;
      struct TypeName { ... };
      union  TypeName { ... };
    """
    if not type_name or not text:
        return False

    tn = re.escape(type_name.strip())

    pat_typedef_struct = re.compile(
        rf'\btypedef\s+struct\b[\s\S]*?\}}\s*{tn}\s*;',
        re.MULTILINE
    )
    pat_typedef_union = re.compile(
        rf'\btypedef\s+union\b[\s\S]*?\}}\s*{tn}\s*;',
        re.MULTILINE
    )
    pat_named_struct = re.compile(rf'\bstruct\s+{tn}\s*\{{', re.MULTILINE)
    pat_named_union = re.compile(rf'\bunion\s+{tn}\s*\{{', re.MULTILINE)

    return bool(
        pat_typedef_struct.search(text)
        or pat_typedef_union.search(text)
        or pat_named_struct.search(text)
        or pat_named_union.search(text)
    )

def _is_struct_or_union_type_anywhere(
    type_name: str,
    c_file_path: str,
    header_index: Dict[str, str],
    include_basenames: List[str],
) -> bool:
    """
    Search:
      - current .c file
      - included headers (in order)
      - fallback: all headers in header_index
    """
    if not type_name:
        return False

    # 0) current .c file
    try:
        if c_file_path and os.path.isfile(c_file_path):
            c_txt = Path(c_file_path).read_text(encoding="utf-8", errors="ignore")
            if _is_struct_or_union_type_name_in_text(type_name, c_txt):
                return True
    except Exception:
        pass

    # 1) included headers first
    for base in include_basenames or []:
        hp = (header_index or {}).get(base)
        if not hp or not os.path.isfile(hp):
            continue
        try:
            txt = Path(hp).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if _is_struct_or_union_type_name_in_text(type_name, txt):
            return True

    # 2) fallback: all headers
    for _base, hp in (header_index or {}).items():
        if not hp or not os.path.isfile(hp):
            continue
        try:
            txt = Path(hp).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if _is_struct_or_union_type_name_in_text(type_name, txt):
            return True

    return False

def generate_test_function_prototypes_block(test_numbers: List[int]) -> str:
    """
    Generate the 'Prototypes for test functions' block.

    Format:
    /* Prototypes for test functions */
    void run_tests();
    void test_214(int);
    ...
    """
    lines = ["/* Prototypes for test functions */", "void run_tests();"]
    for n in test_numbers:
        lines.append(f"void test_{n}(int);")
    return "\n".join(lines) + "\n\n"

def generate_program_entry_point_block(module_name: str) -> str:
    """
    module_name is the base name of the .c file (e.g., 'can_hardware_driver').

    Generates:
    - OPEN_LOG("atest_<module>.ctr", false, 100);
    - START_SCRIPT("<module>", true);
    """
    ctr_name = f"atest_{module_name}.ctr"
    script_name = module_name

    return (
        "/*****************************************************************************/\n"
        "/* Program Entry Point                                                       */\n"
        "/*****************************************************************************/\n"
        "int main()\n"
        "{\n"
        f'    OPEN_LOG("{ctr_name}", false, 100);\n'
        f'    START_SCRIPT("{script_name}", true);\n'
        "\n"
        "    run_tests();\n"
        "\n"
        "    return !END_SCRIPT(true);\n"
        "}\n\n"
    )

def extract_environment_definition_from_atest(project_dir: str, c_file_path: str) -> str:
    """
    Find the corresponding atest file:
      <project_dir>/atest/atest_<module>.c
    and extract the text from the start of the Environment Definition banner
    up to (but not including) the Global Data Definitions banner.

    Returns "" if file/markers not found.
    """
    if not project_dir or not c_file_path:
        return ""

    atest_path = find_atest_file_in_workspace(project_dir, c_file_path)
    if not atest_path:
        return ""
    if not os.path.isfile(atest_path):
        return ""

    try:
        text = Path(atest_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

    # Markers: be tolerant to spacing differences
    env_pat = re.compile(
        r"/\*{5,}\s*\*/\s*\n\s*/\*\s*Environment\s+Definition\s*\*\/\s*\n\s*/\*{5,}\s*\*/",
        re.IGNORECASE
    )
    gdd_pat = re.compile(
        r"/\*{5,}\s*\*/\s*\n\s*/\*\s*Global\s+Data\s+Definitions\s*\*\/\s*\n\s*/\*{5,}\s*\*/",
        re.IGNORECASE
    )

    m_env = env_pat.search(text)
    if not m_env:
        return ""

    m_gdd = gdd_pat.search(text, m_env.end())
    if not m_gdd:
        return ""

    block = text[m_env.start():m_gdd.start()].strip("\n") + "\n"
    return block

def extract_global_definitions_until_prototypes_from_atest(project_dir: str, c_file_path: str) -> str:
    """
    """
    if not project_dir or not c_file_path:
        return ""

    atest_path = find_atest_file_in_workspace(project_dir, c_file_path)
    if not atest_path:
        return ""
    if not os.path.isfile(atest_path):
        return ""

    try:
        text = Path(atest_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

    # Markers: be tolerant to spacing differences
    gdd_pat = re.compile(
        r"/\*{5,}\s*\*/\s*\n\s*/\*\s*Global\s+Data\s+Definitions\s*\*\/\s*\n\s*/\*{5,}\s*\*/",
        re.IGNORECASE
    )

    ptf_pat = re.compile(
        r"/\*\s*Prototypes\s+for\s+test\s+functions\s*\*/",
        re.IGNORECASE
    )

    m_gdd = gdd_pat.search(text)
    if not m_gdd:
        return ""

    m_ptf = ptf_pat.search(text, m_gdd.end())
    if not m_ptf:
        return ""

    block = text[m_gdd.start():m_ptf.start()].strip("\n") + "\n"
    return block

def find_atest_file_in_workspace(project_dir: str, c_file_path: str) -> Optional[str]:
    """
    Given a source C file path, locate its atest file in Workspace layout:
      <project_dir>/Workspace/<module>/Cantata/tests/atest_<module>/atest_<module>.c
    """
    if not project_dir or not c_file_path:
        return None
    module = os.path.splitext(os.path.basename(c_file_path))[0]
    atest_path = os.path.join(
        project_dir, "Workspace", module, "Cantata", "tests",
        f"atest_{module}", f"atest_{module}.c"
    )
    return atest_path if os.path.isfile(atest_path) else None


def resolve_header_from_index(header_index: Dict[str, str], module_name: str) -> str:
    """
    module_name -> '<module_name>.h' lookup in header_index
    Returns '' if not found.
    """
    if not header_index or not module_name:
        return ""
    return header_index.get(f"{module_name}.h", "")

def extract_coverage_analysis_from_atest(project_dir: str, c_file_path: str) -> str:
    """
    Extract the 'Coverage Analysis' block from the corresponding atest file.

    Copies from:
      /*****************************************************************************/
      /* Coverage Analysis                                                         */
      /*****************************************************************************/
    up to (but not including):
      /*****************************************************************************/
      /* Program Entry Point                                                       */
      /*****************************************************************************/

    Returns "" if file/markers not found.
    """
    if not project_dir or not c_file_path:
        return ""

    atest_path = find_atest_file_in_workspace(project_dir, c_file_path)
    if not atest_path:
        return ""
    if not os.path.isfile(atest_path):
        return ""

    try:
        text = Path(atest_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

    cov_pat = re.compile(
        r"/\*{5,}\s*\*/\s*\n\s*/\*\s*Coverage\s+Analysis\s*\*\/\s*\n\s*/\*{5,}\s*\*/",
        re.IGNORECASE
    )
    pep_pat = re.compile(
        r"/\*{5,}\s*\*/\s*\n\s*/\*\s*Program\s+Entry\s+Point\s*\*\/\s*\n\s*/\*{5,}\s*\*/",
        re.IGNORECASE
    )

    m_cov = cov_pat.search(text)
    if not m_cov:
        return ""

    m_pep = pep_pat.search(text, m_cov.end())
    if not m_pep:
        return ""

    block = text[m_cov.start():m_pep.start()].strip("\n") + "\n"
    return block

def _sanitize_c_code_keep_len(s: str) -> str:
    """Replace comments/strings with spaces, keeping length for stable indexing."""
    if not s:
        return s
    out = list(s)
    i, n = 0, len(s)
    while i < n:
        # line comment
        if i + 1 < n and s[i] == "/" and s[i + 1] == "/":
            j = i
            while j < n and s[j] != "\n":
                out[j] = " "
                j += 1
            i = j
            continue
        # block comment
        if i + 1 < n and s[i] == "/" and s[i + 1] == "*":
            j = i
            out[j] = out[j + 1] = " "
            j += 2
            while j + 1 < n and not (s[j] == "*" and s[j + 1] == "/"):
                out[j] = " "
                j += 1
            if j + 1 < n:
                out[j] = out[j + 1] = " "
                j += 2
            i = j
            continue
        # string literal
        if s[i] == '"':
            out[i] = " "
            j = i + 1
            while j < n:
                out[j] = " "
                if s[j] == "\\" and j + 1 < n:
                    out[j + 1] = " "
                    j += 2
                    continue
                if s[j] == '"':
                    j += 1
                    break
                j += 1
            i = j
            continue
        # char literal
        if s[i] == "'":
            out[i] = " "
            j = i + 1
            while j < n:
                out[j] = " "
                if s[j] == "\\" and j + 1 < n:
                    out[j + 1] = " "
                    j += 2
                    continue
                if s[j] == "'":
                    j += 1
                    break
                j += 1
            i = j
            continue
        i += 1
    return "".join(out)


def _find_matching_brace_in_text(s: str, open_idx: int) -> int:
    """Find matching '}' for '{' at open_idx. Works on raw text (best effort)."""
    depth = 0
    i, n = open_idx, len(s)
    while i < n:
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def extract_if_elseif_ladder(func_body: str) -> List[Tuple[str, str]]:
    """
    Returns [(cond, block_text), (cond2, block2), ..., ("__ELSE__", else_block)].
    Only supports braces { } blocks.
    """
    out: List[Tuple[str, str]] = []
    if not func_body:
        return out

    n = len(func_body)

    def skip_ws(i: int) -> int:
        while i < n and func_body[i].isspace():
            i += 1
        return i

    def read_parens(i: int) -> Tuple[Optional[str], int]:
        i = skip_ws(i)
        if i >= n or func_body[i] != "(":
            return None, i
        depth = 0
        start = i + 1
        while i < n:
            if func_body[i] == "(":
                depth += 1
            elif func_body[i] == ")":
                depth -= 1
                if depth == 0:
                    return func_body[start:i], i + 1
            i += 1
        return None, i

    def read_block(i: int) -> Tuple[Optional[str], int]:
        i = skip_ws(i)
        if i >= n or func_body[i] != "{":
            return None, i
        end = _find_matching_brace_in_text(func_body, i)
        if end == -1:
            return None, i
        return func_body[i + 1:end], end + 1

    # Find the first "if"
    clean = _sanitize_c_code_keep_len(func_body)
    m0 = re.search(r"\bif\b", clean)
    if not m0:
        return out

    i = m0.start()
    m_if = re.match(r"\bif\b", clean[i:])
    if not m_if:
        return out
    i += m_if.end()

    cond, i = read_parens(i)
    blk, i = read_block(i)
    if cond is None or blk is None:
        return out
    out.append((cond.strip(), blk))

    # Parse chained else if / else
    while True:
        i = skip_ws(i)
        # must see 'else'
        if not func_body[i:].lstrip().startswith("else"):
            break
        # align i to 'else'
        m_else = re.search(r"\belse\b", func_body[i:])
        if not m_else:
            break
        i = i + m_else.start() + len("else")
        i = skip_ws(i)

        # else if
        if func_body[i:].startswith("if"):
            i += len("if")
            cond, i = read_parens(i)
            blk, i = read_block(i)
            if cond is None or blk is None:
                break
            out.append((cond.strip(), blk))
            continue

        # else
        blk, i = read_block(i)
        if blk is None:
            break
        out.append(("__ELSE__", blk))
        break

    return out

def extract_switch_return_map(func_body: str) -> Dict[str, str]:
    """
    Detect pattern:
        <ret_type> <retvar>;
        switch(param) { case X: { <retvar> = Y; ... } ... default: { <retvar> = Z; ... } }
        return( <retvar> );

    Returns mapping: case_value -> assigned_expr
      e.g. {"EED_APP_RETVAL_SUCCESS": "EEPROM_SUCCESS", "default": "EEPROM_ERROR"}
    """
    if not func_body:
        return {}

    clean = _sanitize_c_code_keep_len(func_body)

    # Find "return( status );" or "return status;"
    mret = re.search(r"\breturn\s*\(?\s*([A-Za-z_]\w*)\s*\)?\s*;", clean)
    if not mret:
        return {}

    retvar = mret.group(1)

    # Find the first switch(...) { ... }
    msw = re.search(r"\bswitch\s*\(([^)]+)\)\s*\{", clean)
    if not msw:
        return {}

    sw_open = msw.end() - 1
    sw_close = _find_matching_brace_in_text(clean, sw_open)
    if sw_close < 0:
        return {}

    sw_body = func_body[sw_open + 1:sw_close]

    # Find case/default blocks and within each block find "<retvar> = <expr>;"
    out: Dict[str, str] = {}

    # Split by labels "case ...:" and "default:"
    label_iter = list(re.finditer(r"\bcase\s+([^:]+)\s*:|\bdefault\s*:", sw_body))
    if not label_iter:
        return {}

    for idx, m in enumerate(label_iter):
        is_default = m.group(0).lstrip().startswith("default")
        label = "default" if is_default else (m.group(1) or "").strip()

        start = m.end()
        end = label_iter[idx + 1].start() if idx + 1 < len(label_iter) else len(sw_body)
        block = sw_body[start:end]

        # find assignment to return variable inside this block
        # keep it simple: first "<retvar> = ...;" wins
        m_asg = re.search(rf"\b{re.escape(retvar)}\s*=\s*([^;]+);", block)
        if m_asg:
            out[label] = m_asg.group(1).strip()

    return out

def _find_condition_var_and_rhs(cond_text: str) -> Optional[Tuple[str, str, str]]:
    """
    Supports:
      var < RHS
      var <= RHS
    where RHS can be:
      - identifier
      - (identifier)
      - (TYPE)identifier
      - extra whitespace/newlines
    Returns (var, op, rhs_symbol).
    """
    if not cond_text:
        return None

    c = cond_text.strip()
    c = re.sub(r"\s+", " ", c)

    # Strip one outer pair of parentheses: "(a < b)" -> "a < b"
    if c.startswith("(") and c.endswith(")"):
        c2 = c[1:-1].strip()
        # only accept if parentheses are "outer"
        if c2.count("(") == c2.count(")"):
            c = c2

    # Allow casts on RHS like "(UINT16)ADV_..." or "(uint32_t) ADV_..."
    # RHS token we want is the final identifier
    m = re.fullmatch(
        r"([A-Za-z_]\w*)\s*(<|<=)\s*(?:\(\s*[A-Za-z_]\w*\s*\)\s*)*\(?\s*([A-Za-z_]\w*)\s*\)?",
        c
    )
    if not m:
        return None

    return m.group(1), m.group(2), m.group(3)

def _read_text_if_exists(path: str) -> str:
    try:
        if path and os.path.isfile(path):
            return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        pass
    return ""


def _collect_defines_and_static_consts(text: str) -> Dict[str, str]:
    """
    Collect simple:
      #define NAME expr
      static volatile const ... NAME = expr;
      static const ... NAME = expr;
    Return: name -> expr (string)
    """
    if not text:
        return {}
    t = _strip_block_comments(text)
    t = _strip_inline_comments(t)
    t = _join_macro_continuations(t)

    out: Dict[str, str] = {}

    for m in re.finditer(r"^[ \t]*#\s*define\s+([A-Za-z_]\w*)\s+(.*)$", t, flags=re.MULTILINE):
        out[m.group(1).strip()] = m.group(2).strip()

    for m in re.finditer(
        r"^[ \t]*static\s+(?:volatile\s+)?const\s+[^=;]+\s+([A-Za-z_]\w*)\s*=\s*([^;]+);",
        t,
        flags=re.MULTILINE,
    ):
        out[m.group(1).strip()] = m.group(2).strip()

    return out


def _eval_c_int_expr(expr: str, symvals: Dict[str, int]) -> Optional[int]:
    """
    Best-effort integer evaluator:
      - strips casts like (UINT32)
      - removes U/UL suffixes on numbers
      - substitutes known symbols
      - allows only digits/operators/() whitespace
    """
    if not expr:
        return None
    e = expr

    # Remove backslash-newline (macro continuations)
    e = e.replace("\\\n", " ")

    # Remove casts like (UINT32)
    e = re.sub(r"\(\s*[A-Za-z_]\w*\s*\)", "", e)

    # Remove numeric suffixes
    e = re.sub(r"(\d+)\s*(U|UL|ULL|L|LL)\b", r"\1", e, flags=re.IGNORECASE)

    # Substitute known symbols
    for k, v in (symvals or {}).items():
        e = re.sub(rf"\b{re.escape(k)}\b", str(int(v)), e)

    # Safety check
    if re.search(r"[^0-9\+\-\*/%\(\)\s]", e):
        return None

    try:
        return int(eval(e, {"__builtins__": {}}, {}))
    except Exception:
        return None

def _collect_ladder_rhs_symbols(ladder: List[Tuple[str, str]]) -> set[str]:
    out = set()
    for cond, _blk in ladder:
        if cond == "__ELSE__":
            continue
        parsed = _find_condition_var_and_rhs(cond)
        if parsed:
            _var, _op, rhs = parsed
            out.add(rhs)
    return out

def _build_symbol_value_db(
    c_file_path: str,
    header_index: Dict[str, str],
    include_basenames: List[str],
    wanted: set[str],
) -> Dict[str, int]:
    sym_expr: Dict[str, str] = {}
    sym_val: Dict[str, int] = {}

    # 0) .c file
    sym_expr.update(_collect_defines_and_static_consts(_read_text_if_exists(c_file_path)))

    # 1) included headers only (NO "all headers" scan)
    for base in include_basenames:
        hp = (header_index or {}).get(base, "")
        if hp:
            sym_expr.update(_collect_defines_and_static_consts(_read_text_if_exists(hp)))

    # Optional pruning: keep only expressions that might matter.
    # Keep any symbol in `wanted` OR any symbol referenced by a wanted expression (1-2 hops).
    if wanted:
        keep = set(wanted)
        # 2-hop dependency expansion (cheap heuristic)
        for _ in range(2):
            for name, expr in sym_expr.items():
                if name in keep:
                    refs = set(re.findall(r"\b[A-Za-z_]\w*\b", expr))
                    keep |= refs
        sym_expr = {k: v for k, v in sym_expr.items() if k in keep}

    # Resolve in passes (reduce 60 -> e.g. 15; with pruning you don't need 60)
    for _ in range(15):
        progressed = False
        for name, expr in sym_expr.items():
            if name in sym_val:
                continue
            v = _eval_c_int_expr(expr, sym_val)
            if v is not None:
                sym_val[name] = v
                progressed = True
        if not progressed:
            break

    return sym_val


def _u16_literal(v: int) -> str:
    v = max(0, min(int(v), 0xFFFF))
    return f"{v}U"


def pick_param_value_for_ladder_rung(
    ladder: List[Tuple[str, str]],
    rung_idx: int,
    param_name: str,
    symvals: Dict[str, int],
) -> Optional[str]:
    """
    Supports ladder conditions: param < RHS_SYMBOL (or <=).
    Chooses a param value that lands specifically in rung_idx.
    """
    if not ladder or rung_idx < 0 or rung_idx >= len(ladder):
        return None

    cond_text = ladder[rung_idx][0]
    if cond_text == "__ELSE__":
        # Choose value that makes all previous conditions false.
        # Use last condition's RHS threshold if we can evaluate it.
        last_real = None
        for j in range(len(ladder) - 1, -1, -1):
            if ladder[j][0] != "__ELSE__":
                last_real = ladder[j][0]
                break
        if not last_real:
            return "0xFFFFU"

        last_parsed = _find_condition_var_and_rhs(last_real)
        if not last_parsed:
            return "0xFFFFU"

        _v, last_op, last_rhs = last_parsed
        last_T = symvals.get(last_rhs)
        if last_T is None:
            return "0xFFFFU"

        # For "<": pick exactly last_T (fails "< last_T")
        # For "<=": pick last_T + 1 (fails "<= last_T")
        pick = int(last_T) if last_op == "<" else int(last_T) + 1
        return _u16_literal(pick)

    parsed = _find_condition_var_and_rhs(cond_text)
    if not parsed:
        return None
    var, op, rhs = parsed
    if var != param_name:
        return None
    if rhs not in symvals:
        return None
    cur_T = int(symvals[rhs])

    if rung_idx == 0:
        # satisfy counts < T0
        return _u16_literal(0)

    # previous rung must be false, current must be true:
    # choose in [prev_T .. cur_T-1]
    prev_cond = ladder[rung_idx - 1][0]
    prev_parsed = _find_condition_var_and_rhs(prev_cond)
    if not prev_parsed:
        return None
    _pvar, _pop, prev_rhs = prev_parsed
    if prev_rhs not in symvals:
        return None
    prev_T = int(symvals[prev_rhs])

    lo = prev_T + 1
    hi = cur_T - 1
    if hi < lo:
        return _u16_literal(lo)

    mid = (lo + hi) // 2
    return _u16_literal(mid)

def _join_macro_continuations(t: str) -> str:
    """
    Join C preprocessor macro lines continued with backslash-newline.
    """
    if not t:
        return t
    return t.replace("\\\r\n", "").replace("\\\n", "")

def _cond_key(cond: str) -> str:
    """Normalize a condition to compare if two condition strings refer to the same logical check."""
    return re.sub(r"\s+", " ", (cond or "").strip())

def _split_top_level_or_conditions(cond: str) -> List[str]:
    """
    Split "(A) || (B) || (C)" into ["(A)", "(B)", "(C)"] while respecting parentheses.
    Very small parser: only needs to be good for typical embedded 'if( (...) || (...) )' patterns.
    """
    s = (cond or "").strip()
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue

        # split on || only at top level
        if depth == 0 and i + 1 < len(s) and s[i:i+2] == "||":
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            i += 2
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)

    return parts

def _split_top_level_and_conditions(cond: str) -> List[str]:
    """
    Split "(A) && (B) && (C)" into ["(A)", "(B)", "(C)"] while respecting parentheses.
    """
    s = (cond or "").strip()
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue

        # split on && only at top level
        if depth == 0 and i + 1 < len(s) and s[i:i+2] == "&&":
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            i += 2
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)

    return parts

def _top_level_operator_kind(cond_text: str) -> str:
    """
    Returns: "OR", "AND", or "SINGLE" based on presence of top-level operators.
    """
    s = (cond_text or "").strip()
    if not s:
        return "SINGLE"

    # A cheap but effective heuristic for your use:
    # if it contains || treat as OR, elif contains && treat as AND.
    # (Your splitters already respect parentheses when extracting terms.)
    if "||" in s:
        return "OR"
    if "&&" in s:
        return "AND"
    return "SINGLE"

def extract_bool_comparisons_from_condition(cond_text: str) -> List[Tuple[str, str]]:
    """
    Extract simple boolean comparisons from a condition, including top-level OR/AND chains.

    Supports:
      (A == TRUE) || (B == TRUE)
      (A == TRUE) && (B == TRUE)
      (A == FALSE)
    Returns: [(lhs, rhs), ...] where rhs is TRUE/FALSE
    """
    if not cond_text:
        return []

    s = cond_text.strip()

    # Decide whether this is an OR-chain or AND-chain at top level.
    # Prefer splitting by || if present, else by && if present, else treat as single.
    if "||" in s:
        terms = _split_top_level_or_conditions(s)
    elif "&&" in s:
        terms = _split_top_level_and_conditions(s)
    else:
        terms = [s]

    out: List[Tuple[str, str]] = []
    for t in terms:
        tt = t.strip()
        if tt.startswith("(") and tt.endswith(")"):
            tt = tt[1:-1].strip()

        m = re.fullmatch(r"(.+?)\s*==\s*(TRUE|FALSE)\s*", tt)
        if not m:
            continue

        lhs = m.group(1).strip()
        rhs = m.group(2).strip()
        out.append((lhs, rhs))

    return out

def extract_simple_eq_ident_comparisons_from_condition(cond_text: str) -> List[Tuple[str, str]]:
    """
    Extract simple comparisons like:
        (RTA_Runtime_Context == IOC_RUNTIME_CONTEXT_RUN)
    from a top-level OR/AND chain.

    Returns [(lhs, rhs_ident), ...]
    """
    if not cond_text:
        return []

    s = cond_text.strip()

    # split by top-level || or && (same logic you used in extract_bool_comparisons_from_condition)
    if "||" in s:
        terms = _split_top_level_or_conditions(s)
    elif "&&" in s:
        terms = _split_top_level_and_conditions(s)
    else:
        terms = [s]

    out: List[Tuple[str, str]] = []
    for t in terms:
        tt = t.strip()
        if tt.startswith("(") and tt.endswith(")"):
            tt = tt[1:-1].strip()

        # lhs == IDENT (NOT TRUE/FALSE)
        m = re.fullmatch(r"(.+?)\s*==\s*([A-Za-z_]\w*)\s*", tt)
        if not m:
            continue

        lhs = m.group(1).strip()
        rhs = m.group(2).strip()

        if rhs.upper() in ("TRUE", "FALSE"):
            continue

        out.append((lhs, rhs))

    return out

def emit_condition_inputs_for_test(
    *,
    lines: List[str],
    cond: str,
    focus_truth: bool,
) -> None:
    """
    Emit ONLY input assignments (no expected_*) so `cond` evaluates to focus_truth.

    Supports top-level:
      - boolean comparisons: (lhs == TRUE/FALSE)
      - ident comparisons:   (lhs == SOME_ENUM / SOME_MACRO)
      - also tries single simple conditions via _cond_to_assignment_simple fallback

    IMPORTANT:
      - Never emits expected_... lines (prevents duplicates / expected_expected bugs)
      - Never assigns to any 'expected_*' variable even if it appears in text
    """
    cond = (cond or "").strip()
    if not cond:
        return

    op_kind = _top_level_operator_kind(cond)

    # Extract terms
    bool_terms = extract_bool_comparisons_from_condition(cond)
    eq_ident_terms = extract_simple_eq_ident_comparisons_from_condition(cond)

    # Safety: never treat expected_* as a LHS input
    def _is_expected_lhs(lhs: str) -> bool:
        return (lhs or "").strip().startswith("expected_")

    bool_terms = [(l, r) for (l, r) in bool_terms if not _is_expected_lhs(l)]
    eq_ident_terms = [(l, r) for (l, r) in eq_ident_terms if not _is_expected_lhs(l)]

    def emit(lhs: str, rhs: str) -> None:
        lines.append(f"    {lhs} = {rhs};")

    if focus_truth:
        # TRUE case:
        # - AND => must satisfy all terms
        # - OR  => satisfying all terms is also OK (simple)
        for lhs, rhs in bool_terms:
            emit(lhs, rhs)
        for lhs, rhs in eq_ident_terms:
            emit(lhs, rhs)

        # Fallback for single conditions not caught above
        if not bool_terms and not eq_ident_terms:
            a = _cond_to_assignment_simple(cond, truth=True)
            if a:
                lhs, rhs = a
                if not _is_expected_lhs(lhs):
                    emit(lhs, rhs)
        return

    # FALSE case:
    if op_kind == "OR":
        # OR is false only if all terms are false
        for lhs, rhs in bool_terms:
            emit(lhs, _flip_true_false(rhs))

        # Can't safely invert eq-ident without knowing enum domain
        for lhs, rhs in eq_ident_terms:
            lines.append(f"    /* TODO: make ({lhs} == {rhs}) false for OR-false coverage */")

        if not bool_terms and not eq_ident_terms:
            a = _cond_to_assignment_simple(cond, truth=False)
            if a:
                lhs, rhs = a
                if not _is_expected_lhs(lhs):
                    emit(lhs, rhs)
        return

    if op_kind == "AND":
        # AND is false if ANY one term is false.
        # Make everything TRUE, then flip ONE boolean term.
        for lhs, rhs in eq_ident_terms:
            emit(lhs, rhs)

        for lhs, rhs in bool_terms:
            emit(lhs, rhs)

        if bool_terms:
            lhs0, rhs0 = bool_terms[0]
            emit(lhs0, _flip_true_false(rhs0))
        else:
            # No boolean terms; only eq-ident terms. Cannot safely force false.
            for lhs, rhs in eq_ident_terms:
                lines.append(f"    /* TODO: make ({lhs} == {rhs}) false for AND-false coverage */")

        if not bool_terms and not eq_ident_terms:
            a = _cond_to_assignment_simple(cond, truth=False)
            if a:
                lhs, rhs = a
                if not _is_expected_lhs(lhs):
                    emit(lhs, rhs)
        return

    # SINGLE
    a = _cond_to_assignment_simple(cond, truth=focus_truth)
    if a:
        lhs, rhs = a
        if not _is_expected_lhs(lhs):
            emit(lhs, rhs)

def _find_assigned_lhs_in_block(block_text: str) -> set[str]:
    """
    Find LHS identifiers/paths that are assigned in a C block.
    Very lightweight: catches patterns like:
        X = ...
        A.B.c = ...
        A->B = ...
    """
    if not block_text:
        return set()

    # strip comments (best-effort)
    t = _strip_block_comments(block_text)
    t = _strip_inline_comments(t)

    out: set[str] = set()

    # match "lhs =" (avoid ==, <=, >=, !=)
    for m in re.finditer(r"([A-Za-z_]\w*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*)*)\s*=(?!=)", t):
        lhs = re.sub(r"\s+", "", m.group(1))  # normalize spaces around . / ->
        if lhs:
            out.add(lhs)

    return out

def _flip_true_false(rhs: str) -> str:
    r = (rhs or "").strip().upper()
    if r == "TRUE":
        return "FALSE"
    if r == "FALSE":
        return "TRUE"
    return rhs

def emit_condition_assignments_for_test(
    *,
    lines: List[str],
    cond: str,
    focus_truth: bool,
    block_text_for_assigned_filter: str = "",
) -> None:
    """
    Emit assignments into `lines` to make `cond` evaluate to focus_truth.

    Handles top-level OR/AND chains for:
      - boolean comparisons:        (lhs == TRUE/FALSE)
      - identifier comparisons:     (lhs == SOME_ENUM / SOME_MACRO)

    IMPORTANT:
      If block_text_for_assigned_filter is provided, we will NOT emit assignments
      for LHS that are assigned inside that block (so outputs are not treated as inputs).
    """
    cond = cond or ""
    op_kind = _top_level_operator_kind(cond)

    bool_terms = extract_bool_comparisons_from_condition(cond)
    eq_ident_terms = extract_simple_eq_ident_comparisons_from_condition(cond)

    # Filter out "outputs" (things assigned inside the objective block)
    assigned_lhs = _find_assigned_lhs_in_block(block_text_for_assigned_filter or "")

    def _norm_lhs(x: str) -> str:
        return re.sub(r"\s+", "", (x or ""))

    bool_terms = [(lhs, rhs) for (lhs, rhs) in bool_terms if _norm_lhs(lhs) not in assigned_lhs]
    eq_ident_terms = [(lhs, rhs) for (lhs, rhs) in eq_ident_terms if _norm_lhs(lhs) not in assigned_lhs]

    def _emit(lhs: str, rhs: str) -> None:
        lines.append(f"    {lhs} = {rhs};")
        lines.append(f"    expected_{lhs} = {rhs};")

    if focus_truth is True:
        # TRUE-case:
        # AND => must satisfy ALL terms; OR => satisfying all is also OK (simple & safe).
        for lhs, rhs in bool_terms:
            _emit(lhs, rhs)
        for lhs, rhs in eq_ident_terms:
            _emit(lhs, rhs)
        return

    # FALSE-case
    if op_kind == "OR":
        # OR is FALSE only if ALL boolean terms are FALSE
        for lhs, rhs in bool_terms:
            _emit(lhs, _flip_true_false(rhs))

        # eq-ident OR terms cannot be safely inverted without enum-domain knowledge
        for lhs, rhs in eq_ident_terms:
            lines.append(f"    /* TODO: make ({lhs} == {rhs}) false for OR-false coverage */")
        return

    if op_kind == "AND":
        # AND is FALSE if ANY one term is FALSE.
        # Strategy:
        # - satisfy all eq-ident terms
        # - satisfy all boolean terms
        # - then flip exactly ONE boolean term (first one) to break the AND
        for lhs, rhs in eq_ident_terms:
            _emit(lhs, rhs)

        if bool_terms:
            # emit all as TRUE first
            for lhs, rhs in bool_terms:
                _emit(lhs, rhs)

            # now flip the first boolean term -> makes AND false deterministically
            lhs0, rhs0 = bool_terms[0]
            _emit(lhs0, _flip_true_false(rhs0))
        else:
            # No boolean term to flip; try simple handler as fallback
            a = _cond_to_assignment_simple(cond, truth=False)
            if a:
                lhs, rhs = a
                _emit(lhs, rhs)
            else:
                for lhs, rhs in eq_ident_terms:
                    lines.append(f"    /* TODO: make ({lhs} == {rhs}) false for AND-false coverage */")
        return

    # SINGLE
    a = _cond_to_assignment_simple(cond, truth=focus_truth)
    if a:
        lhs, rhs = a
        _emit(lhs, rhs)

def _cond_to_assignment_simple(cond_txt: str, truth: bool):
    """
    Convert a *simple* C condition into a forced assignment (lhs, rhs) so the path is taken.

    Supports:
      - var == TRUE / FALSE / 1U / 0U
      - var != TRUE / FALSE / 1U / 0U
      - var (treated as var != 0)
      - !var

    Also supports bitfield / struct access names like:
      SSI_Ford_Active_Configuration.B.xfc

    NOTE:
      This is intentionally lightweight and safe. If it can't understand the condition,
      it returns None and the generator will continue without forcing.
    """
    if not cond_txt:
        return None

    s = cond_txt.strip()

    # remove outer parentheses repeatedly: (((x))) -> x
    def _strip_outer_parens(x: str) -> str:
        x = (x or "").strip()
        while x.startswith("(") and x.endswith(")"):
            inner = x[1:-1].strip()
            # only strip if parentheses are balanced (simple check)
            if inner.count("(") == inner.count(")"):
                x = inner
            else:
                break
        return x

    s = _strip_outer_parens(s)

    # normalize booleans we will emit (match your generator style)
    def _emit_bool(val: bool) -> str:
        return "TRUE" if val else "FALSE"

    # Accept fairly broad "lhs" tokens: ident(.ident)*, ident->ident, and bitfields
    # We also allow array index on lhs (your code supports maps elsewhere).
    lhs_re = r"([A-Za-z_]\w*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*)*(?:\s*\[[^\]]+\])?)"
    const_re = r"(TRUE|FALSE|0U|1U|0|1)"

    # 1) Equality / inequality forms: lhs == CONST, lhs != CONST
    m = re.match(rf"^\s*{lhs_re}\s*(==|!=)\s*{const_re}\s*$", s)
    if m:
        lhs, op, rhs = m.group(1).strip(), m.group(2), m.group(3).strip()

        # determine the boolean value of rhs
        rhs_bool = None
        if rhs in ("TRUE", "1", "1U"):
            rhs_bool = True
        elif rhs in ("FALSE", "0", "0U"):
            rhs_bool = False

        if rhs_bool is None:
            return None

        # We want the expression (lhs op rhs) to evaluate to `truth`.
        # If op == '==': need lhs = rhs when truth=True, lhs = !rhs when truth=False
        # If op == '!=': need lhs = !rhs when truth=True, lhs = rhs when truth=False
        if op == "==":
            desired_lhs_bool = rhs_bool if truth else (not rhs_bool)
        else:  # "!="
            desired_lhs_bool = (not rhs_bool) if truth else rhs_bool

        return (lhs, _emit_bool(desired_lhs_bool))

    # 2) Unary NOT: !lhs
    m = re.match(rf"^\s*!\s*{lhs_re}\s*$", s)
    if m:
        lhs = m.group(1).strip()
        # (!lhs) should be truth => lhs must be FALSE; else TRUE
        desired = False if truth else True
        return (lhs, _emit_bool(desired))

    # 3) Bare variable used as condition: lhs
    # Treat as "lhs != 0" => for truth make TRUE, for false make FALSE
    m = re.match(rf"^\s*{lhs_re}\s*$", s)
    if m:
        lhs = m.group(1).strip()
        return (lhs, _emit_bool(truth))

    return None

def _strip_preprocessor_regions(code: str) -> str:
    """Remove #if/#endif regions (best effort) so brace parsing doesn't get confused."""
    if not code:
        return ""
    lines = code.splitlines(True)
    out = []
    skip = 0
    for ln in lines:
        if re.match(r'^[ \t]*#(if|ifdef|ifndef)\b', ln):
            skip += 1
            continue
        if skip and re.match(r'^[ \t]*#endif\b', ln):
            skip -= 1
            continue
        if skip:
            continue
        out.append(ln)
    return "".join(out)

def _extract_nested_objectives(func_body: str) -> List[Dict[str, Any]]:
    """
    Build objectives for ALL top-level if/else-if/else chains in this block and nested blocks.

    Key behavior:
    - Generate TRUE/FALSE for inner conditions (focus is the inner condition).
    - Do NOT generate a separate "true case" for a parent condition if reaching the inner condition already implies it.
    - DO generate the parent FALSE/else path if it exists.

    Output items:
      {
        "guards": [(cond_string, truth_bool), ...],
        "focus": "<condition string>",
        "focus_truth": True/False,
        "block": "<block text>"
      }
    """
    body = _strip_preprocessor_regions(func_body or "")
    chains = _find_top_level_if_chains(body)
    if not chains:
        return []

    out: List[Dict[str, Any]] = []

    for chain in chains:
        branches = chain["branches"]          # [(cond, blk), ...]
        else_blk = chain.get("else", "")      # else block or ""

        # For each branch i (if / else-if):
        for i, (cond_i, blk_i) in enumerate(branches):
            guards: List[Tuple[str, bool]] = []

            # all previous branch conditions must be FALSE
            for j in range(i):
                guards.append((branches[j][0], False))

            # this branch condition must be TRUE
            guards.append((cond_i, True))

            # Recurse inside this branch block: it may contain multiple inner if chains
            nested = _extract_nested_objectives(blk_i)
            if nested:
                # IMPORTANT: do NOT create objective for cond_i itself (it's implied by reaching nested focus)
                for nobj in nested:
                    out.append({
                        "guards": guards + nobj["guards"],
                        "focus": nobj["focus"],
                        "focus_truth": nobj["focus_truth"],
                        "block": nobj["block"],
                    })
            else:
                # leaf: this condition itself is the focus TRUE case
                out.append({
                    "guards": guards,
                    "focus": cond_i,
                    "focus_truth": True,
                    "block": blk_i,
                })

        # ELSE path: all branch conditions FALSE
        # IMPORTANT: generate else objective even if else block is empty/comment-only,
        # because you still need the "false case" coverage for the last else-if condition.
        if else_blk is not None:
            else_guards: List[Tuple[str, bool]] = [(c, False) for (c, _b) in branches]

            nested_else = _extract_nested_objectives(else_blk)
            if nested_else:
                # else doesn't have its own condition; keep focus from nested
                for nobj in nested_else:
                    out.append({
                        "guards": else_guards + nobj["guards"],
                        "focus": nobj["focus"],
                        "focus_truth": nobj["focus_truth"],
                        "block": nobj["block"],
                    })
            else:
                # else leaf: focus is the LAST condition, FALSE case
                focus_cond = branches[-1][0]
                out.append({
                    "guards": else_guards,
                    "focus": focus_cond,
                    "focus_truth": False,
                    "block": else_blk,
                })


    # De-dup objectives by (focus, focus_truth, guards)
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for o in out:
        key = (
            _cond_key(o["focus"]),
            bool(o["focus_truth"]),
            tuple((_cond_key(c), bool(t)) for c, t in o["guards"]),
        )
        if key not in seen:
            seen.add(key)
            uniq.append(o)

    return uniq

def _find_matching_paren_idx(s: str, open_idx: int) -> int:
    """Return index of matching ')' for '(' at open_idx; -1 if not found."""
    depth = 0
    i, n = open_idx, len(s)
    while i < n:
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        elif ch == '"':
            i += 1
            while i < n and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
        elif ch == "'":
            i += 1
            while i < n and s[i] != "'":
                i += 2 if s[i] == "\\" else 1
        i += 1
    return -1


def _skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i].isspace():
        i += 1
    return i


def _extract_block_after_brace(s: str, brace_idx: int) -> tuple[str, int]:
    """
    brace_idx points to '{'
    returns (block_inside, idx_after_closing_brace)
    """
    end = _find_matching_brace_in_text(s, brace_idx)
    if end == -1:
        return "", brace_idx + 1
    return s[brace_idx + 1:end], end + 1


def _parse_if_chain_at(s: str, if_idx: int) -> tuple[list[tuple[str, str]], str, int]:
    """
    Parse:
      if (cond){blk}
      [else if (cond2){blk2}]*    (NOTE: must be 'else if' with braces)
      [else {blkE}]
    Returns (branches, else_block, next_index)
      branches = [(cond, blk), ...] for if/else-if only
      else_block = blk for else ("" if none)
      next_index = index after the whole chain
    """
    n = len(s)
    i = if_idx
    if not s[i:].startswith("if"):
        return [], "", if_idx + 1

    i += 2
    i = _skip_ws(s, i)
    if i >= n or s[i] != "(":
        return [], "", if_idx + 2

    pend = _find_matching_paren_idx(s, i)
    if pend == -1:
        return [], "", if_idx + 2

    cond = s[i + 1:pend].strip()
    i = pend + 1
    i = _skip_ws(s, i)
    if i >= n or s[i] != "{":
        return [], "", i

    blk, i = _extract_block_after_brace(s, i)
    branches = [(cond, blk)]

    else_blk = ""
    while True:
        i0 = _skip_ws(s, i)
        if not s[i0:].startswith("else"):
            i = i0
            break

        i = i0 + 4
        i = _skip_ws(s, i)

        if s[i:].startswith("if"):
            i += 2
            i = _skip_ws(s, i)
            if i >= n or s[i] != "(":
                break
            pend = _find_matching_paren_idx(s, i)
            if pend == -1:
                break
            cond2 = s[i + 1:pend].strip()
            i = pend + 1
            i = _skip_ws(s, i)
            if i >= n or s[i] != "{":
                break
            blk2, i = _extract_block_after_brace(s, i)
            branches.append((cond2, blk2))
            continue

        # else { ... }
        if i < n and s[i] == "{":
            else_blk, i = _extract_block_after_brace(s, i)
        break

    return branches, else_blk, i


def _find_top_level_if_chains(s: str) -> list[dict]:
    """
    Return all top-level if-chains in this block (not nested inside braces).
    Each dict:
      {
        "start": idx,
        "end": idx_after_chain,
        "branches": [(cond, blk), ...],   # if + else-if
        "else": else_blk                  # else block content or ""
      }
    """
    clean = _sanitize_c_code_keep_len(s)
    out = []
    depth = 0
    i, n = 0, len(s)

    while i < n:
        ch = clean[i]
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth = max(0, depth - 1)
            i += 1
            continue

        if depth == 0:
            if re.match(r"\bif\b", clean[i:]):
                branches, else_blk, end = _parse_if_chain_at(s, i)
                if branches:
                    out.append({"start": i, "end": end, "branches": branches, "else": else_blk})
                    i = end
                    continue

        i += 1

    return out

# ------------------- FULL GENERATOR BODY (verbatim move) -------------------

def generate_tests_cantata_style(func, c_file_path, structs, atest_macros, atest_ranges,
                                 root_path=None, seq_start: int = 1, header_index: Optional[Dict[str, str]] = None):
    import os
    import re
    from typing import List, Tuple, Optional, Dict

    if not root_path or not isinstance(root_path, str) or not os.path.isdir(root_path):
        raise ValueError(
            "ERROR: root_path is not set or does not exist. Please select your project folder containing Header/ and Source/.")

    # Ensure minimal module-level registries exist for map allocation (do not change other file-level logic)
    if '_GLOBAL_FILE_MAPS' not in globals():
        globals()['_GLOBAL_FILE_MAPS'] = {}  # module -> { map_var_name: (base_type, size) }
    if '_GLOBAL_MAP_COUNTERS' not in globals():
        globals()['_GLOBAL_MAP_COUNTERS'] = {}  # module -> { param_name: next_counter_int }
    GLOBAL_FILE_MAPS: Dict[str, Dict[str, Tuple[str, int]]] = globals()['_GLOBAL_FILE_MAPS']
    GLOBAL_MAP_COUNTERS: Dict[str, Dict[str, int]] = globals()['_GLOBAL_MAP_COUNTERS']

    DEFAULT_MAP_SIZE = 255

    # --- CANTATA Headers ---
    def generate_atest_header(source_c_filename, author=None, generated_on=None):
        import getpass
        import datetime

        if author is None:
            author = getpass.getuser()  # system username

        if generated_on is None:
            generated_on = datetime.datetime.now().strftime("%d-%b-%Y %H:%M:%S")

        # Compose the atest filename (output file name convention, e.g., atest_IOconfig.c)
        base = os.path.basename(source_c_filename)
        module = os.path.splitext(base)[0]
        atest_filename = f"atest_{module}.c"

        header = f"""\
    /*****************************************************************************/
    /*                            Cantata Test Script                            */
    /*****************************************************************************/
    /*
     *    Filename: {atest_filename}
     *    Author: {author}
     *    Generated on: {generated_on}
     *    Generated from: {base}
     */
    """
        return header

    # --- FIX: define strip_type_qualifiers so it's available when used later ---
    def strip_type_qualifiers(type_str):
        """
        Remove common C qualifiers from a type string so tests declare the base type.
        Example: "static const U8" -> "U8"
        """
        type_str = type_str or ""
        words = type_str.split()
        data_type = []
        for w in words:
            if w.lower() not in ["static", "inline", "const", "volatile", "restrict"]:
                data_type.append(w)
        return " ".join(data_type).strip()

    def _ladder_is_param_driven(ladder, param_names: set[str]) -> bool:
        """
        Ladder mode is ONLY for cases like: if (param < MACRO) else if (param < MACRO) ...
        Not for: localVar == TRUE, funcCall() == ..., etc.
        """
        if not ladder:
            return False

        for cond, _blk in ladder:
            if cond == "__ELSE__":
                continue
            parsed = _find_condition_var_and_rhs(cond)
            if not parsed:
                return False
            var, op, rhs = parsed
            if op not in ("<", "<="):
                return False
            if var not in param_names:
                return False
            if not rhs:
                return False
        return True

    # --- small helper: valid C identifier check (non-invasive) ---
    def _is_valid_ident(name: Optional[str]) -> bool:
        return bool(name and re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name))

    name = func.get('name')
    if not name:
        return []

    module = os.path.splitext(os.path.basename(c_file_path))[0]
    header_file = resolve_header_from_index(header_index or {}, module)
    func_body, full_code = get_function_source(c_file_path, func.get('name'))
    if not isinstance(func_body, str):
        func_body = ""
    if not isinstance(full_code, str):
        full_code = ""

    # --- IF/ELSEIF ladder extraction (MISSING IN NEW FILE) ---
    ladder = extract_if_elseif_ladder(func_body or "")
    wanted_rhs = _collect_ladder_rhs_symbols(ladder)
    symvals = _build_symbol_value_db(c_file_path, header_index or {}, _parse_includes_from_c_file(c_file_path),
                                     wanted_rhs)
    param_names = {nm for _ty, nm in func.get("args", []) if nm}
    ladder_param_driven = _ladder_is_param_driven(ladder, param_names)

    # Ladder mode is ONLY for cases like:
    #   if (param < MACRO) { ... }
    #   else if (param < MACRO) { ... }
    # Not for:
    #   if (localVar == TRUE) ...
    #   if (funcCall() == ...) ...
    def _ladder_is_param_driven(ladder, param_names: set[str]) -> bool:
        if not ladder:
            return False
        for cond, _blk in ladder:
            if cond == "__ELSE__":
                continue
            parsed = _find_condition_var_and_rhs(cond)
            if not parsed:
                return False
            var, op, rhs = parsed
            if op not in ("<", "<="):
                return False
            if var not in param_names:
                return False
            if not rhs:
                return False
        return True

    # Only keep ladder if it is param-driven, else disable ladder feature

    include_bases = _parse_includes_from_c_file(c_file_path)

    # only collect RHS symbols from ladder conditions that we kept
    wanted_rhs = _collect_ladder_rhs_symbols(ladder)

    symvals = _build_symbol_value_db(
        c_file_path,
        header_index or {},
        include_bases,
        wanted_rhs,
    )

    # --- Utility: Strip conditional compilation blocks (#if, #ifdef, etc.) ---
    def strip_conditional_and_pragma_blocks(code: str) -> str:
        # Remove all #if/#ifdef/#ifndef ... #endif blocks
        def remove_conditional_blocks(text):
            lines = text.splitlines(keepends=True)
            output = []
            skip = 0
            for line in lines:
                if re.match(r'^[ \t]*#(if|ifdef|ifndef)\b', line):
                    skip += 1
                    continue
                if skip > 0 and re.match(r'^[ \t]*#endif\b', line):
                    skip -= 1
                    continue
                if skip > 0:
                    continue
                output.append(line)
            return ''.join(output)

        code = remove_conditional_blocks(code)
        code = re.sub(r'^.*#pragma.*$\n?', '', code, flags=re.MULTILINE)
        return code

    # --- This is your original function call extractor, updated to use the stripped code ---
    def extract_function_calls(block_text, exclude_names=None):
        if exclude_names is None:
            exclude_names = set()
        block_text = strip_conditional_and_pragma_blocks(block_text)
        block_text = re.sub(r'/\*.*?\*/', '', block_text, flags=re.DOTALL)
        block_text = re.sub(r'//.*', '', block_text)
        block_text = re.sub(r'"(?:\\.|[^"])*"', '""', block_text)
        block_text = re.sub(r"'(?:\\.|[^'])*'", "''", block_text)
        tokens = re.findall(r'\b([A-Za-z_]\w*)\s*\(', block_text)
        blacklist = {'if', 'for', 'while', 'switch', 'return', 'sizeof', 'case', 'else', 'do'}
        calls = []
        for t in tokens:
            if t in exclude_names or t in blacklist or t.isupper():
                continue
            calls.append(t)
        return calls

    def param_is_denominator(param_name, func_body):
        if re.search(r'\/\s*' + re.escape(param_name), func_body) or re.search(r'%\s*' + re.escape(param_name),
                                                                               func_body):
            return True
        return False

    def _is_enum_type_name_in_text(type_name: str, text: str) -> bool:
        if not type_name or not text:
            return False

        tn = re.escape(type_name.strip())
        pat_typedef_enum = re.compile(
            rf'\btypedef\s+enum\b[\s\S]*?\}}\s*{tn}\s*;',
            re.MULTILINE
        )
        pat_named_enum = re.compile(
            rf'\benum\s+{tn}\s*\{{',
            re.MULTILINE
        )
        return bool(pat_typedef_enum.search(text) or pat_named_enum.search(text))

    # ---------- ENUM DB (NEW) ----------
    def collect_enum_members_from_text(code: str) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        if not code:
            return out

        def _clean_enum_item(raw: str) -> str:
            s = raw.strip()
            if not s:
                return ""
            # remove comments
            s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
            s = re.sub(r"//.*", "", s)
            # remove initializer
            if "=" in s:
                s = s.split("=", 1)[0]
            s = s.strip()
            # keep only identifier
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\b", s)
            return m.group(1) if m else ""

        def _members_from_block(members_block: str) -> List[str]:
            members: List[str] = []
            for part in members_block.split(","):
                ident = _clean_enum_item(part)
                if ident and ident not in members:
                    members.append(ident)
            return members

        # typedef enum { ... } Name;
        for m in re.finditer(
                r"typedef\s+enum\s*\{([^}]*)\}\s*([A-Za-z_][A-Za-z0-9_]*)\s*;",
                code, re.MULTILINE | re.DOTALL
        ):
            members_block, enum_name = m.groups()
            members = _members_from_block(members_block)
            if members:
                out[enum_name] = members

        # enum Name { ... };
        for m in re.finditer(
                r"enum\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{([^}]*)\}\s*;",
                code, re.MULTILINE | re.DOTALL
        ):
            enum_name, members_block = m.groups()
            members = _members_from_block(members_block)
            if members:
                out[enum_name] = members

        return out

    def build_enum_db(c_file_path: str, header_index: Dict[str, str]) -> Dict[str, List[str]]:
        enum_db: Dict[str, List[str]] = {}

        # 0) scan the .c file
        try:
            if c_file_path and os.path.isfile(c_file_path):
                c_txt = Path(c_file_path).read_text(encoding="utf-8", errors="ignore")
                enum_db.update(collect_enum_members_from_text(c_txt))
        except Exception:
            pass

        # 1) scan headers included by the .c (in order)
        include_bases = _parse_includes_from_c_file(c_file_path)
        for base in include_bases:
            hp = (header_index or {}).get(base)
            if not hp or not os.path.isfile(hp):
                continue
            try:
                txt = Path(hp).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            enum_db.update(collect_enum_members_from_text(txt))

        # 2) fallback: scan all headers
        for _base, hp in (header_index or {}).items():
            if not hp or not os.path.isfile(hp):
                continue
            try:
                txt = Path(hp).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            enum_db.update(collect_enum_members_from_text(txt))

        return enum_db

    def collect_enum_first_members(header_file):
        enum_map = {}
        if not header_file or not os.path.isfile(header_file):
            return enum_map
        with open(header_file, encoding="utf-8", errors="ignore") as f:
            code = f.read()
            for m in re.finditer(r'typedef\s+enum\s*\{([^}]*)\}\s*([A-Za-z_][A-Za-z0-9_]*)\s*;', code,
                                 re.MULTILINE | re.DOTALL):
                members_block, enum_name = m.groups()
                members = [mem.strip().split('=')[0].strip() for mem in members_block.split(',') if mem.strip()]
                if members:
                    enum_map[enum_name] = members[0]
            for m in re.finditer(r'enum\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{([^}]*)\}\s*;', code, re.MULTILINE | re.DOTALL):
                enum_name, members_block = m.groups()
                members = [mem.strip().split('=')[0].strip() for mem in members_block.split(',') if mem.strip()]
                if members:
                    enum_map[enum_name] = members[0]
        return enum_map

    def get_enum_initializer(ty, enum_map):
        clean_ty = re.sub(r'\b(const|volatile)\b', '', ty).replace('*', '').replace(' ', '').strip()
        if clean_ty in enum_map:
            return enum_map[clean_ty]
        for key in enum_map:
            if clean_ty == key or clean_ty.endswith(key) or key.endswith(clean_ty):
                return enum_map[key]
        return "0U"

    enum_map = collect_enum_first_members(header_file)
    enum_db = build_enum_db(c_file_path, header_index or {})

    def _enum_members_for_type(ty: str) -> Optional[List[str]]:
        clean_ty = re.sub(r'\b(const|volatile)\b', '', (ty or '')).replace('*', '').replace(' ', '').strip()
        members = enum_db.get(clean_ty)
        return members if members else None

    # ---------- your existing helpers ----------
    def collect_static_const_objects(code):
        matches = re.findall(
            r'^\s*static\s+const\s+[A-Za-z0-9_]+\s+([A-Za-z0-9_]+)\s*(?:\[.*?\])?\s*=',
            code, re.MULTILINE
        )
        return set(matches)

    static_const_objects = collect_static_const_objects(full_code)

    def collect_all_aliases(code, static_const_objects):
        aliases = set()
        for obj in static_const_objects:
            pat1 = rf'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*{re.escape(obj)}[^\n;]*'
            for m in re.finditer(pat1, code):
                aliases.add(m.group(1))
            pat2 = rf'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*{re.escape(obj)}\s*[\[\]\.\->]'
            for m in re.finditer(pat2, code):
                aliases.add(m.group(1))
        changed = True
        while changed:
            changed = False
            for alias in list(aliases):
                pat = rf'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*{re.escape(alias)}\b'
                for m in re.finditer(pat, code):
                    new_alias = m.group(1)
                    if new_alias not in aliases:
                        aliases.add(new_alias)
                        changed = True
        return aliases

    all_aliases = collect_all_aliases(full_code, static_const_objects)

    def get_lhs_root(lhs):
        s = lhs.strip()
        s = re.sub(r'^[*&]+', '', s)
        if s.startswith('(') and s.endswith(')'):
            s = s[1:-1].strip()
        return re.split(r'(?:\.|->|\[)', s, maxsplit=1)[0].strip()

    def skip_if_const(lhs, rhs):
        root_lhs = get_lhs_root(lhs)
        if root_lhs in static_const_objects or root_lhs in all_aliases:
            return True
        if any(obj in lhs for obj in static_const_objects):
            return True
        if any(alias in lhs for alias in all_aliases):
            return True
        if any(obj in rhs for obj in static_const_objects):
            return True
        return False

    def extract_assignments(block_text):
        """
        Extract assignments like: lhs = rhs;
        Returns list[(lhs, rhs)].

        IMPORTANT:
        - block_text can be None if the if/else parser couldn't extract a block.
        - Never crash here; just return [].
        """
        if not isinstance(block_text, (str, bytes)):
            return []

        assigns = []
        for m in re.finditer(r'([A-Za-z_][\w\.->\[\]]*)\s*=\s*([^;]+);', block_text):
            lhs = m.group(1).strip()
            rhs = m.group(2).strip()
            assigns.append((lhs, rhs))
        return assigns

    def find_matching_brace(s, open_idx):
        depth = 0
        n = len(s)
        i = open_idx
        while i < n:
            ch = s[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return i
            elif ch == '"':
                i += 1
                while i < n and s[i] != '"':
                    i += 2 if s[i] == '\\' else 1
            i += 1
        return -1

    def is_static_local(varname, func_body):
        pattern = re.compile(r'\bstatic\s+[A-Za-z_][\w\s\*]*\b' + re.escape(varname) + r'\b')
        return bool(pattern.search(func_body))

    def is_local(varname, func_body):
        pattern = re.compile(r'\b([A-Za-z_][\w\s\*]*)\b' + re.escape(varname) + r'\b\s*(=|;)')
        return bool(pattern.search(func_body)) and not is_static_local(varname, func_body)

    def _is_guard_assignable(lhs: str) -> bool:
        """
        Guards should only assign things the test can control:
        - NOT local variables
        - NOT static locals
        """
        if not lhs:
            return False
        root = lhs.split('.', 1)[0].split('->', 1)[0].split('[', 1)[0].strip()
        if is_local(root, func_body) or is_static_local(root, func_body):
            return False
        return True

    def is_macro_name(varname):
        return bool(re.match(r'^[A-Z0-9_]+(_[0-9]+)?$', varname))

    def make_test_names(lhs):
        base = lhs.split('.', 1)[0].split('->')[0].split('[')[0]
        if is_macro_name(base):
            test_base = base + '_1'
            return test_base + lhs[len(base):], 'expected_' + test_base + lhs[len(base):]
        else:
            test_base = base
            return test_base + lhs[len(base):], 'expected_' + test_base + lhs[len(base):]

    def _find_matching_paren(s: str, open_idx: int) -> int:
        """Return index of matching ')' for '(' at open_idx, or -1."""
        depth = 0
        i, n = open_idx, len(s)
        while i < n:
            ch = s[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
            elif ch == '"':
                i += 1
                while i < n and s[i] != '"':
                    i += 2 if s[i] == "\\" else 1
            elif ch == "'":
                i += 1
                while i < n and s[i] != "'":
                    i += 2 if s[i] == "\\" else 1
            i += 1
        return -1

    def extract_branch_blocks(func_body):
        m = re.search(r'\bif\s*\((.*?)\)\s*\{', func_body, re.DOTALL)
        if not m:
            return func_body, '', '', ''

        pre_block = func_body[:m.start()]
        if_start = m.end() - 1
        if_end = find_matching_brace(func_body, if_start)
        if_end = if_end if if_end != -1 else len(func_body) - 1
        if_block = func_body[if_start + 1:if_end]

        else_block = ''
        post_block = ''
        pos = if_end + 1

        # NEW: match "else {" OR "else if (...) {"
        m_else = re.search(r'\belse\b', func_body[pos:])
        if m_else:
            epos = pos + m_else.end()
            # skip whitespace
            while epos < len(func_body) and func_body[epos].isspace():
                epos += 1

            # else if (...) { ... }
            if func_body.startswith("if", epos):
                # find the '{' after the else-if condition
                m_elif = re.search(r'\bif\s*\((.*?)\)\s*\{', func_body[epos:], re.DOTALL)
                if m_elif:
                    brace_idx = epos + m_elif.end() - 1
                    e_end = find_matching_brace(func_body, brace_idx)
                    if e_end != -1:
                        else_block = func_body[brace_idx + 1:e_end]
                        post_block = func_body[e_end + 1:]
                        return pre_block, if_block, else_block, post_block

            # plain else { ... }
            m_else_brace = re.search(r'\belse\s*\{', func_body[pos:])
            if m_else_brace:
                else_start = pos + m_else_brace.end() - 1
                else_end = find_matching_brace(func_body, else_start)
                if else_end != -1:
                    else_block = func_body[else_start + 1:else_end]
                    post_block = func_body[else_end + 1:]
                else:
                    post_block = func_body[if_end + 1:]
            else:
                post_block = func_body[if_end + 1:]
        else:
            post_block = func_body[if_end + 1:]

        return pre_block, if_block, else_block, post_block

    def extract_switch_cases(body):
        cases = []
        for m in re.finditer(r'\bswitch\s*\(([^)]+)\)\s*\{', body):
            switch_var = m.group(1).strip()
            start = m.end() - 1
            end = find_matching_brace(body, start)
            if end == -1:
                continue
            switch_body = body[start + 1:end]
            for cm in re.finditer(r'\bcase\s+([^\:]+)\s*:', switch_body):
                case_val = cm.group(1).strip()
                case_start = cm.end()
                next_case = re.search(r'\b(case\s+[^\:]+:|default\s*:)', switch_body[case_start:])
                case_end = case_start + (next_case.start() if next_case else len(switch_body[case_start:]))
                case_block = switch_body[case_start:case_end]
                cases.append({'kind': 'case', 'switch_var': switch_var, 'case_val': case_val, 'case_block': case_block,
                              'switch_body': switch_body})
            for dm in re.finditer(r'\bdefault\s*:', switch_body):
                case_start = dm.end()
                next_case = re.search(r'\b(case\s+[^\:]+:)', switch_body[case_start:])
                case_end = case_start + (next_case.start() if next_case else len(switch_body[case_start:]))
                case_block = switch_body[case_start:case_end]
                cases.append(
                    {'kind': 'default', 'switch_var': switch_var, 'case_val': 'default', 'case_block': case_block,
                     'switch_body': switch_body})
        return cases

    # Local registrations for this function (map var name -> (type,size)) and substitutions param->map_var
    local_map_decls: Dict[str, Tuple[str, int]] = {}
    local_map_names: Dict[str, str] = {}

    def _alloc_and_register_map(param_name: str, base_type: str) -> str:
        if not _is_valid_ident(param_name):
            return "NULL"

        mod_maps = GLOBAL_FILE_MAPS.setdefault(module, {})
        mod_counters = GLOBAL_MAP_COUNTERS.setdefault(module, {})
        cnt = mod_counters.get(param_name, 0)
        while True:
            suffix = "" if cnt == 0 else f"_{cnt}"
            candidate = f"map_{param_name}{suffix}"
            if candidate not in mod_maps and candidate not in local_map_decls:
                break
            cnt += 1
        mod_counters[param_name] = cnt + 1
        bt = base_type.replace('*', '').strip() or 'unsigned int'
        mod_maps[candidate] = (bt, DEFAULT_MAP_SIZE)
        local_map_decls[candidate] = (bt, DEFAULT_MAP_SIZE)
        local_map_names[param_name] = candidate
        return candidate

    def pointer_map_subst_local(lhs, pointer_args):
        m = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\s*(\[.*\])', lhs)
        if m:
            base, idx = m.group(1), m.group(2)
            if base in local_map_names:
                return f"{local_map_names[base]}{idx}"
            module_maps = GLOBAL_FILE_MAPS.get(module, {})
            candidate = f"map_{base}"
            if candidate in module_maps:
                return f"{candidate}{idx}"
            return lhs
        return lhs

    def expected_pointer_map_local(lhs, pointer_args):
        m = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\s*(\[.*\])', lhs)
        if m:
            base, idx = m.group(1), m.group(2)
            if base in local_map_names:
                return f"expected_{local_map_names[base]}{idx}"
            candidate = f"expected_map_{base}"
            module_maps = GLOBAL_FILE_MAPS.get(module, {})
            if candidate.replace("expected_", "") in module_maps:
                return f"{candidate}{idx}"
            return f"expected_{lhs}"
        return lhs

    static_const_objects = collect_static_const_objects(full_code)
    all_aliases = collect_all_aliases(full_code, static_const_objects)

    # Use the robust ladder parser (handles if/else if/else + formatting)
    ladder = extract_if_elseif_ladder(func_body or "")

    # NEW: nested if/else objectives
    nested_objectives = _extract_nested_objectives(func_body or "")

    # Split into classic if/else blocks (single if-else, not full ladder)
    pre_block, if_block, else_block, post_block = extract_branch_blocks(func_body or "")

    # Ensure these are strings (never tuples/None)
    pre_block = pre_block or ""
    if_block = if_block or ""
    else_block = else_block or ""
    post_block = post_block or ""

    # Extract assignments from each region
    pre_assigns = extract_assignments(pre_block)
    if_assigns = extract_assignments(if_block)
    else_assigns = extract_assignments(else_block)
    post_assigns = extract_assignments(post_block)

    switch_cases = extract_switch_cases(func_body or "")
    switch_ret_map = extract_switch_return_map(func_body or "")
    seq = seq_start
    tests = []

    is_static = func.get("is_static", False)
    raw_ret_type = (func.get("return_type") or "").strip()
    base_ret_type = strip_type_qualifiers(raw_ret_type)
    include_basenames = _parse_includes_from_c_file(c_file_path)

    # --- Build macro/const value DB for ladder thresholds ---
    wanted_rhs = _collect_ladder_rhs_symbols(ladder) if ladder_param_driven else set()
    symvals = _build_symbol_value_db(
        c_file_path=c_file_path,
        header_index=header_index or {},
        include_basenames=include_basenames,
        wanted=wanted_rhs,
    )

    typedef_db = _build_typedef_db(header_index or {}, include_basenames)

    resolved_ret = ""
    ret_check_macro = None

    if raw_ret_type and raw_ret_type.lower() != "void":
        if not _is_pointer_type(raw_ret_type):
            resolved_ret = _resolve_typedef_chain(raw_ret_type, typedef_db)
            ret_check_macro = _cantata_check_macro_for_resolved_type(resolved_ret)

            if not ret_check_macro:
                enum_in_header = False
                enum_in_source = False

                try:
                    if header_file and os.path.isfile(header_file):
                        header_txt = Path(header_file).read_text(encoding="utf-8", errors="ignore")
                        enum_in_header = _is_enum_type_name_in_text(resolved_ret or raw_ret_type, header_txt)
                except Exception:
                    pass

                try:
                    if c_file_path and os.path.isfile(c_file_path):
                        c_txt = Path(c_file_path).read_text(encoding="utf-8", errors="ignore")
                        enum_in_source = _is_enum_type_name_in_text(resolved_ret or raw_ret_type, c_txt)
                except Exception:
                    pass

                if enum_in_header or enum_in_source:
                    ret_check_macro = "CHECK_S_INT"

            if not ret_check_macro:
                struct_union_found = False

                try:
                    if header_file and os.path.isfile(header_file):
                        header_txt = Path(header_file).read_text(encoding="utf-8", errors="ignore")
                        if _is_struct_or_union_type_name_in_text(resolved_ret or raw_ret_type, header_txt):
                            struct_union_found = True
                except Exception:
                    pass

                try:
                    if c_file_path and os.path.isfile(c_file_path):
                        c_txt = Path(c_file_path).read_text(encoding="utf-8", errors="ignore")
                        if _is_struct_or_union_type_name_in_text(resolved_ret or raw_ret_type, c_txt):
                            struct_union_found = True
                except Exception:
                    pass

                if not struct_union_found:
                    for _base, hp in (header_index or {}).items():
                        if not hp or not os.path.isfile(hp):
                            continue
                        try:
                            txt = Path(hp).read_text(encoding="utf-8", errors="ignore")
                        except Exception:
                            continue
                        if _is_struct_or_union_type_name_in_text(resolved_ret or raw_ret_type, txt):
                            struct_union_found = True
                            break

                if struct_union_found:
                    ret_check_macro = _NO_RETURN_CHECK

    call_target = f"ACCESS_FUNCTION({module}, {name})" if is_static else name
    param_types_and_names = [(ty, nm) for ty, nm in func.get('args', [])]

    sweep_params = [
        (ty, nm, test_values_for_type(ty))
        for ty, nm in param_types_and_names
        if test_values_for_type(ty) and ('*' not in (ty or ''))
    ]
    pointer_args = [nm for ty, nm in param_types_and_names if '*' in (ty or '')]
    call_line_plain = f"{call_target}({', '.join([nm for _, nm in param_types_and_names])});"

    # ---------- ENUM PARAM SWEEP PLAN (NEW) ----------
    # For now: support the common case "one enum parameter" (your example).
    enum_sweep_params = []
    for ty, nm in param_types_and_names:
        members = _enum_members_for_type(ty)
        if members and nm:
            enum_sweep_params.append((ty, nm, members))

    # choose one enum param to sweep (first one found)
    enum_sweep_param = enum_sweep_params[0] if enum_sweep_params else None
    enum_cases = []
    if enum_sweep_param:
        _ety, _enm, _members = enum_sweep_param
        enum_cases = list(range(len(_members)))
    else:
        enum_cases = [None]

    def get_default_val(ty):
        return get_enum_initializer(ty, enum_map)

    # PATCH: render_param_decls now supports enum_idx (NEW)
    def render_param_decls(
            sweep_idx=None,
            enum_idx: Optional[int] = None,
            code_block="",
            skip_params: Optional[set] = None,
            force_param_values: Optional[Dict[str, str]] = None,  # NEW
    ):
        import random
        out = []
        param_decl_dict = {}

        skip_params = skip_params or set()
        force_param_values = force_param_values or {}  # FIX: define once, before loop (prevents UnboundLocalError)

        ret_type = (func.get('return_type') or '').strip()
        ret_type_nc = re.sub(r'\bconst\b', '', ret_type).replace('  ', ' ').strip()

        # If caller is forcing the expected return value (switch mapping etc.),
        # don't do min/mid/max return sweep.
        forced_expected_rv = force_param_values.get("expected_returnValue")

        if ret_type and ret_type_nc.lower() != 'void':
            if not any(re.match(rf"^\s*{re.escape(ret_type_nc)}\s+returnValue\s*;", ln) for ln in out):
                out.append(f"{ret_type_nc} returnValue;")

            if forced_expected_rv is not None:
                exp_line = f"{ret_type_nc} expected_returnValue = {forced_expected_rv};"
            else:
                ret_vals = test_values_for_type(ret_type_nc)
                if ret_vals and sweep_idx is not None:
                    exp_line = f"{ret_type_nc} expected_returnValue = {ret_vals[sweep_idx]};"
                else:
                    if "float" in ret_type_nc.lower() or "double" in ret_type_nc.lower():
                        exp_line = f"{ret_type_nc} expected_returnValue = 0.0;"
                    else:
                        exp_line = f"{ret_type_nc} expected_returnValue = 0U;"

            if not any(re.match(rf"^\s*{re.escape(ret_type_nc)}\s+expected_returnValue\s*=", ln) for ln in out):
                out.append(exp_line)

        # setup chosen enum sweep
        enum_ty = enum_nm = None
        enum_members: List[str] = []
        if enum_sweep_param:
            enum_ty, enum_nm, enum_members = enum_sweep_param

        for ty, nm in param_types_and_names:
            ty_nc = re.sub(r'\bconst\b', '', ty).replace('  ', ' ').strip()

            if nm in skip_params:
                continue

            # Forced param values have highest priority
            if nm in force_param_values:
                out.append(f"{ty_nc} {nm} = {force_param_values[nm]};")
                param_decl_dict[nm] = force_param_values[nm]
                continue

            # pointer params
            if '*' in ty_nc:
                if _is_valid_ident(nm):
                    if nm not in local_map_names:
                        base_type = ty_nc.replace('*', '').strip() or 'unsigned int'
                        map_var = _alloc_and_register_map(nm, base_type)
                        local_map_names[nm] = map_var
                    else:
                        map_var = local_map_names[nm]
                    out.append(f"{ty_nc} {nm} = &{map_var}[0];")
                    param_decl_dict[nm] = map_var
                else:
                    out.append(f"{ty_nc} {nm} = NULL;")
                    param_decl_dict[nm] = "NULL"
                continue

            # enum params
            members = _enum_members_for_type(ty_nc)
            if members and nm:
                chosen = members[0]
                if enum_sweep_param and nm == enum_nm and enum_idx is not None:
                    chosen = members[min(enum_idx, len(members) - 1)]
                out.append(f"{ty_nc} {nm} = {chosen};")
                param_decl_dict[nm] = chosen
                continue

            # numeric sweep params (min/random/max)
            vals = test_values_for_type(ty)
            if param_is_denominator(nm, func_body):
                if ty_nc.startswith('U'):
                    vals = ['1U', f'0x{random.randint(2, 255):02X}U', f'0x{255:02X}U']
                else:
                    vals = ['1U', f'0x{random.randint(2, 127):02X}U', f'0x{127:02X}U']

            if vals and sweep_idx is not None:
                out.append(f"{ty_nc} {nm} = {vals[sweep_idx]};")
                param_decl_dict[nm] = vals[sweep_idx]
            else:
                val = get_default_val(ty_nc)
                out.append(f"{ty_nc} {nm} = {val};")
                param_decl_dict[nm] = val

        return out, param_decl_dict

    raw_condition_selectors = extract_condition_selectors(func_body)
    condition_selectors = filter_condition_selectors(func, func_body, raw_condition_selectors)

    def add_selector_lines(assign_lines, exp_lines, branch_val):
        for sel in condition_selectors:
            assign_lines.append(f"{sel} = {branch_val};")
            exp_lines.append(f"expected_{sel} = {branch_val};")

    def pointer_param_loop_assignment_patch(assign_lines, exp_lines, func_body, pointer_args, param_decl_dict):
        skip_assigns = set()
        loop_patterns = [
            r'for\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*0\s*;\s*\1\s*<\s*([A-Za-z_][A-Za-z0-9_]*)\s*;',
            r'while\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*<\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)'
        ]
        for pat in loop_patterns:
            for lm in re.finditer(pat, func_body):
                idx_var, bound_param = lm.group(1), lm.group(2)
                assign_pat = r'([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*' + re.escape(idx_var) + r'\s*\]\s*=\s*([^\;]+);'
                for m in re.finditer(assign_pat, func_body):
                    dest_arr = m.group(1)
                    rhs = m.group(2).strip()
                    if dest_arr not in pointer_args:
                        continue

                    bound_val = param_decl_dict.get(bound_param, 3)
                    if isinstance(bound_val, str):
                        mhex = re.match(r'0[xX]([0-9a-fA-F]+)U?', bound_val)
                        if mhex:
                            bound_val = int(mhex.group(1), 16)
                        else:
                            try:
                                bound_val = int(bound_val)
                            except Exception:
                                bound_val = 3
                    try:
                        bound_val = int(bound_val)
                    except Exception:
                        bound_val = 3

                    src_m = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*' + re.escape(idx_var) + r'\s*\]', rhs)
                    src_arr = src_m.group(1) if src_m else None
                    rhs_is_src_index = src_arr in pointer_args if src_arr else False

                    if rhs_is_src_index:
                        for i in range(bound_val):
                            assign_lines.append(f"map_{src_arr}[{i}] = (UINT8){i};")

                    for i in range(bound_val):
                        assign_lines.append(f"map_{dest_arr}[{i}] = 0U;")
                        if rhs_is_src_index:
                            exp_lines.append(f"expected_map_{dest_arr}[{i}] = map_{src_arr}[{i}];")
                        else:
                            exp_lines.append(f"expected_map_{dest_arr}[{i}] = {rhs.replace(idx_var, str(i))};")

                    skip_assigns.add((dest_arr, idx_var))
        return skip_assigns

    # --- Generation of tests (main body) ---
    # ---------------- IF/ELSEIF LADDER MODE (PARAM-THRESHOLD) ----------------
    # Only use ladder mode for patterns like:
    #   if (counts < TH1) { ... }
    #   else if (counts < TH2) { ... }
    #   ...
    #   else { ... }
    #
    # DO NOT use this for CHD_Init()-style:
    #   if (x == TRUE) { ... } else { ... }
    #
    # This block assumes you already computed:
    #   ladder = extract_if_elseif_ladder(func_body)
    #   include_basenames = _parse_includes_from_c_file(c_file_path)
    #   symvals = _build_symbol_value_db(... wanted RHS symbols ...)
    #   param_types_and_names
    #   call_line_plain
    #   etc.

    def _is_threshold_ladder(ladder: List[Tuple[str, str]]) -> bool:
        """
        True only if every non-ELSE condition looks like: <ident> < <ident>  OR  <ident> <= <ident>
        This prevents CHD_Init from being mis-classified as a threshold ladder.
        """
        if not ladder:
            return False
        for cond, _blk in ladder:
            if cond == "__ELSE__":
                continue
            if _find_condition_var_and_rhs(cond) is None:
                return False
        return True

    def _ladder_drives_a_param(ladder: List[Tuple[str, str]], param_names: set[str]) -> Optional[str]:
        """
        If all rung conditions compare the SAME variable (and it is a function parameter),
        return that variable name. Else return None.
        """
        vars_seen = []
        for cond, _blk in ladder:
            if cond == "__ELSE__":
                continue
            parsed = _find_condition_var_and_rhs(cond)
            if not parsed:
                return None
            var, _op, _rhs = parsed
            vars_seen.append(var)

        if not vars_seen:
            return None

        # must be same variable for the entire ladder
        v0 = vars_seen[0]
        if any(v != v0 for v in vars_seen):
            return None

        return v0 if v0 in param_names else None

    name = func.get("name")
    if not name:
        return []

    # define header early so it always exists
    header = make_cantata_header(func, c_file_path, purpose=f"Auto-generated Cantata tests for {name}.")

    # Decide whether to use ladder mode or not
    param_names = {nm for _ty, nm in param_types_and_names if nm}


    # If we get here: either no ladder, or it wasn't a threshold ladder.
    # Let the existing old logic handle CHD_Init() and other patterns.
    if ladder_param_driven and ladder:
        # Determine which param drives the ladder (from first condition)
        first_cond = next((c for c, _b in ladder if c != "__ELSE__"), "")
        parsed = _find_condition_var_and_rhs(first_cond)
        ladder_param = parsed[0] if parsed else None

        for rung_idx, (cond, blk) in enumerate(ladder):
            # Choose value for ladder_param that lands in this rung
            forced = {}
            if ladder_param:
                pv = pick_param_value_for_ladder_rung(ladder, rung_idx, ladder_param, symvals)
                if pv is not None:
                    forced[ladder_param] = pv

            for sweep_idx in range(3 if sweep_params else 1):
                decl_lines, param_decl_dict = render_param_decls(
                    sweep_idx if sweep_params else None,
                    None,
                    blk,
                    force_param_values=forced,  # NEW
                )

                # now generate assign_lines/exp_lines based on blk or based on extracted assignments in blk
                # (use your existing extract_assignments + make_test_names logic)
                rung_assigns = extract_assignments(blk)
                assign_lines = []
                exp_lines = []

                for lhs, rhs in rung_assigns:
                    if skip_if_const(lhs, rhs):
                        continue
                    test_lhs, expected_lhs = make_test_names(lhs)
                    assign_lines.append(f"{test_lhs} = {special_assignment_value(rhs)};")
                    exp_lines.append(f"{expected_lhs} = {rhs};")

                pre_lines = decl_lines + assign_lines + exp_lines

                call_list = extract_function_calls(blk, exclude_names={func.get('name')})
                expected_calls_str = ";".join(f"{c}#1" for c in call_list)

                title = f"{seq}_MC: {name}"
                desc = f"Created to test ladder rung {rung_idx + 1}: {cond}"

                mc_lines = [
                    f"void test_{seq}(int doIt){{",
                    "if (doIt) {",
                    "    /* Set global data */",
                    "    initialise_global_data();",
                    "    /* Set expected values for global data checks */",
                    "    initialise_expected_global_data();",
                    "    {",
                    "    /* Test case data declarations */",
                ]
                for _l in pre_lines:
                    mc_lines.append("    " + _l)
                mc_lines += [
                    "",
                    f"    START_TEST(\"{title}\",",
                    f"               \"{desc}\");",
                    "",
                    "        /* Expected Call Sequence  */",
                    f"        EXPECTED_CALLS(\"{expected_calls_str}\");",
                    "",
                    "            /* Call SUT */",
                    f"            {call_line_plain}",
                    "",
                    "            /* Test case checks */",
                ]
                check_line = _emit_return_check_line(raw_ret_type, ret_check_macro, resolved_ret)
                if check_line:
                    mc_lines.append(f"            {check_line}")
                mc_lines += [
                    "            /* Checks on global data */",
                    "            check_global_data();",
                    "        END_CALLS();",
                    "    END_TEST();",
                    "}}}"
                ]
                tests.append("\n".join(mc_lines))
                seq += 1

    elif if_assigns or else_assigns:
        # NEW: generate tests for nested if/else-if/else paths (instead of only outer if true/false)

        # If the nested objectives extractor returned nothing, fall back to old behavior (safety).
        if not nested_objectives:
            nested_objectives = []

        for obj in nested_objectives:
            # Build guard assignments for this objective (forces the path)
            guard_assigns: List[Tuple[str, str]] = []

            # Build guard assignment *lines* (as text), then parse them back into tuples.
            guard_lines: List[str] = []

            focus_block_text = obj.get("block", "")

            for cond_txt, truth in obj.get("guards", []):
                emit_condition_assignments_for_test(
                    lines=guard_lines,
                    cond=cond_txt,
                    focus_truth=bool(truth),
                    block_text_for_assigned_filter=focus_block_text,
                )

            # Convert "    LHS = RHS;" lines into tuples (LHS, RHS)
            guard_assigns: List[Tuple[str, str]] = []
            for gl in guard_lines:
                m = re.match(
                    r"^\s*([A-Za-z_]\w*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*)*(?:\s*\[[^\]]+\])?)\s*=\s*(.+?)\s*;\s*$", gl)
                if m:
                    guard_assigns.append((m.group(1).strip(), m.group(2).strip()))



            # Decide what code block to analyze for assignments/calls
            # - use the leaf block if present
            # - fallback to full func_body
            leaf_block = obj.get("block") or ""
            branch_code = leaf_block if leaf_block.strip() else (func_body or "")

            # Extract assignments only from the leaf block (this is important for nested)

            leaf_assigns = extract_assignments(leaf_block) if leaf_block.strip() else []

            # Keep order: prelude first, then leaf
            assignments = pre_assigns + leaf_assigns + post_assigns

            for sweep_idx in range(3 if sweep_params else 1):
                decl_lines, param_decl_dict = render_param_decls(
                    sweep_idx if sweep_params else None,
                    None,
                    branch_code,
                )

                assign_lines: List[str] = []
                exp_lines: List[str] = []

                # Apply guard assignments FIRST (these force the path)
                for lhs, rhs in guard_assigns:
                    # Don't try to assign local variables (not controllable)
                    if not _is_guard_assignable(lhs):
                        continue

                    # IMPORTANT:
                    # Guards must set the REAL variable (inputs), not the expected_ mirror.
                    # Otherwise the if-condition won't change and you'll get "false only".
                    #
                    # Also prevent expected_expected_... by never passing expected_* into make_test_names.
                    if lhs.strip().startswith("expected_"):
                        # If it already came as expected_, skip (guards should never target expected_)
                        continue

                    if any(lhs.startswith(arg + "[") for arg in pointer_args):
                        real_lhs = pointer_map_subst_local(lhs, pointer_args)
                        exp_lhs = expected_pointer_map_local(lhs, pointer_args)
                        assign_lines.append(f"{real_lhs} = {rhs};")
                        exp_lines.append(f"{exp_lhs} = {rhs};")
                    else:
                        # real variable assignment
                        assign_lines.append(f"{lhs} = {rhs};")
                        # expected mirror assignment
                        exp_lines.append(f"expected_{lhs} = {rhs};")

                # Existing pointer loop patch (keep it)
                skip_assigns = pointer_param_loop_assignment_patch(
                    assign_lines, exp_lines, func_body, pointer_args, param_decl_dict
                )

                # Now apply assignments inside the *leaf* block
                # Now apply assignments inside the *leaf* block
                for lhs, rhs in assignments:
                    if skip_if_const(lhs, rhs):
                        continue

                    varname = lhs.split('.')[0].split('->')[0]
                    if is_local(varname, func_body) and not is_static_local(varname, func_body):
                        continue
                    if lhs.strip() in condition_selectors:
                        continue

                    m_arr_assign = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\[(\w+)\]', lhs)
                    if m_arr_assign:
                        arr_name, arr_idx = m_arr_assign.group(1), m_arr_assign.group(2)
                        if (arr_name, arr_idx) in skip_assigns:
                            continue

                    # LEAF ASSIGNMENTS MUST NOT BE FLIPPED.
                    # The leaf block represents what the SUT does; we want expected == actual after the call.
                    if any(lhs.startswith(arg + "[") for arg in pointer_args):
                        real_lhs = pointer_map_subst_local(lhs, pointer_args)
                        exp_lhs = expected_pointer_map_local(lhs, pointer_args)
                        _append_unique(assign_lines, f"{real_lhs} = {rhs};")
                        _append_unique(exp_lines, f"{exp_lhs} = {rhs};")
                    else:
                        # Do NOT call make_test_names() if it might transform unexpectedly.
                        # For non-pointer, just assign and mirror expected_ directly.
                        _append_unique(assign_lines, f"{lhs} = {rhs};")
                        _append_unique(exp_lines, f"expected_{lhs} = {rhs};")

                pre_lines = decl_lines + assign_lines + exp_lines

                # Expected calls derived from the leaf block only
                call_list = extract_function_calls(branch_code, exclude_names={func.get('name')})
                expected_calls_str = ";".join(f"{c}#1" for c in call_list)

                # Description: focus condition true/false if provided
                focus = obj.get("focus") or ""
                focus_key = _cond_key(focus) if focus else ""

                # IMPORTANT:
                # - For "leaf TRUE" objectives, focus condition is NOT necessarily present in guards
                #   (it may be implied). So use obj["focus_truth"] as source of truth.
                focus_truth = obj.get("focus_truth", None)

                # If focus_truth wasn't present (older objs), try to infer from guards
                if focus_truth is None and focus_key:
                    for c, t in obj.get("guards", []):
                        if _cond_key(c) == focus_key:
                            focus_truth = t
                            break

                # If still unknown and we have a focus, default to False (else-style leaf)
                if focus and focus_truth is None:
                    focus_truth = False

                if focus:
                    if focus_truth is True:
                        desc = f"Created to test the true case of if({focus})"
                    else:
                        desc = f"Created to test the false case of if({focus})"
                else:
                    # fallback label
                    desc = f"Created to test nested path: {obj.get('label', 'path')}"

                title = f"{seq}_MC: {func.get('name')}"

                mc_lines = [
                    f"void test_{seq}(int doIt){{",
                    "if (doIt) {",
                    "    /* Set global data */",
                    "    initialise_global_data();",
                    "    /* Set expected values for global data checks */",
                    "    initialise_expected_global_data();",
                    "    {",
                    "    /* Test case data declarations */",
                ]
                for _l in pre_lines:
                    mc_lines.append("    " + _l)

                mc_lines += [
                    "",
                    f"    START_TEST(\"{title}\",",
                    f"               \"{desc}\");",
                    "",
                    "        /* Expected Call Sequence  */",
                    f"        EXPECTED_CALLS(\"{expected_calls_str}\");",
                    "",
                    "            /* Call SUT */",
                    f"            {call_line_plain}",
                    "",
                    "            /* Test case checks */",
                ]

                check_line = _emit_return_check_line(raw_ret_type, ret_check_macro, resolved_ret)
                if check_line:
                    mc_lines.append(f"            {check_line}")

                mc_lines += [
                    "            /* Checks on global data */",
                    "            check_global_data();",
                    "        END_CALLS();",
                    "    END_TEST();",
                    "}}}",
                ]

                tests.append("\n".join(mc_lines))
                seq += 1

    elif switch_cases:
        # (kept your existing switch logic as-is)
        switch_var = switch_cases[0]['switch_var'] if switch_cases else None
        switch_ty = None
        for ty, nm in param_types_and_names:
            if nm == switch_var:
                switch_ty = ty.strip()
                break

        enum_members: List[str] = []
        enum_sources = [header_file, c_file_path]
        for enum_src in enum_sources:
            if enum_src and os.path.isfile(enum_src):
                with open(enum_src, 'r', encoding="utf-8", errors="ignore") as f:
                    code = f.read()
                for m_enum in re.finditer(r'enum\s+(?:[A-Za-z_][A-Za-z0-9_]*)?\s*\{([^}]*)\}', code,
                                          re.MULTILINE | re.DOTALL):
                    enum_body = m_enum.group(1)
                    for part in enum_body.split(','):
                        item = part.strip()
                        if not item:
                            continue
                        item = re.sub(r'/\*.*?\*/', '', item, flags=re.DOTALL)
                        item = re.sub(r'//.*', '', item)
                        item = item.split('=')[0].strip()
                        item = item.rstrip(',').strip()
                        if item and item not in enum_members:
                            enum_members.append(item)

        if not enum_members and switch_ty:
            if switch_ty in enum_map:
                enum_members = [enum_map[switch_ty]]

        used_case_vals = set()
        for sc in switch_cases:
            if sc['case_val'] != "default":
                val = sc['case_val']
                val_clean = (re.sub(r'/\*.*?\*/', '', val).split('=')[0].split()[0].strip().rstrip(','))
                used_case_vals.add(val_clean)

        for sc in switch_cases:
            case_sweep_indices = [None] if switch_ret_map else list(range(3 if sweep_params else 1))
            for sweep_idx in case_sweep_indices:
                pre_lines: List[str] = []
                case_val = sc['case_val']
                case_block = sc['case_block']
                # Skip ALL params here because switch-case logic below re-declares them.
                _all_param_names = {nm for _ty, nm in param_types_and_names if nm}
                decl_lines, param_decl_dict = render_param_decls(
                    sweep_idx if sweep_params else None,
                    None,
                    case_block,
                    skip_params=_all_param_names,
                )
                # If this switch assigns to a local var that is returned, set expected_returnValue per-case
                if switch_ret_map and raw_ret_type and raw_ret_type.lower() != "void":
                    cv = sc["case_val"]
                    key = "default" if cv == "default" else cv

                    expected_expr = None
                    if key in switch_ret_map:
                        expected_expr = switch_ret_map[key]
                    elif "default" in switch_ret_map:
                        # fallback to default mapping if case has no explicit mapping
                        expected_expr = switch_ret_map["default"]

                    if expected_expr:
                        # Replace any existing expected_returnValue line
                        decl_lines = [ln for ln in decl_lines if not re.search(r"\bexpected_returnValue\b", ln)]
                        ret_type_nc = re.sub(r"\bconst\b", "", (func.get("return_type") or "")).replace("  ",
                                                                                                        " ").strip()
                        decl_lines.append(f"{ret_type_nc} expected_returnValue = {expected_expr};")
                pre_lines.extend(decl_lines)

                if case_val == "default":
                    desc = "Created to test the default case"

                    # Decide default value for the switch variable ONLY (not for all params)
                    # 1) Find the declared type of the switch var from the function args
                    switch_var_type = ""
                    for ty, nm in func.get("args", []):
                        if nm == switch_var:
                            switch_var_type = (ty or "").strip()
                            break

                    # 2) Only use enum member if switch var type is actually an enum type
                    #    (otherwise fall back to numeric literal)
                    if _should_use_enum_value_for_switch_var(switch_var_type, c_file_path, header_index or {}):
                        # Your existing enum-member logic is OK in this branch
                        unused = [m for m in enum_members if m not in used_case_vals]
                        test_value = unused[0] if unused else (enum_members[-1] if enum_members else "0U")
                    else:
                        test_value = _default_value_for_switch_var(switch_var_type)  # e.g. "0xFFU" or "0U"
                else:
                    test_value = case_val
                    desc = f"Created to test the case: {case_val}"

                for ty, nm in func.get('args', []):
                    vals = test_values_for_type(ty)
                    if nm == switch_var:
                        pre_lines.append(f"{ty} {nm} = {test_value};")
                    elif vals and sweep_idx is not None:
                        pre_lines.append(f"{ty} {nm} = {vals[sweep_idx]};")
                    elif '*' in ty:
                        if _is_valid_ident(nm):
                            if nm not in local_map_names:
                                base_type = ty.replace('*', '').strip() or 'unsigned int'
                                map_var = _alloc_and_register_map(nm, base_type)
                                if map_var != "NULL":
                                    local_map_names[nm] = map_var
                                    local_map_decls[map_var] = (base_type, DEFAULT_MAP_SIZE)
                            pre_lines.append(f"{ty} {nm} = {local_map_names.get(nm, 'NULL')};")
                        else:
                            pre_lines.append(f"{ty} {nm} = NULL;")
                    else:
                        default_val = get_enum_initializer(ty, enum_map)
                        pre_lines.append(f"{ty} {nm} = {default_val};")

                call_list = extract_function_calls(case_block, exclude_names={name})
                expected_calls_str = ";".join(f"{c}#1" for c in call_list)

                mc_lines = []
                mc_lines.append(f"void test_{seq}(int doIt){{")
                mc_lines.append("if (doIt) {")
                mc_lines.append("    /* Set global data */")
                mc_lines.append("    initialise_global_data();")
                mc_lines.append("    /* Set expected values for global data checks */")
                mc_lines.append("    initialise_expected_global_data();")
                mc_lines.append("    {")
                mc_lines.append("    /* Test case data declarations */")
                for _l in pre_lines:
                    mc_lines.append('    ' + _l)
                mc_lines.append("")
                mc_lines.append(f"    START_TEST(\"{seq}_MC: {name}\",")
                mc_lines.append(f"               \"{desc}\");")
                mc_lines.append("")
                mc_lines.append("        /* Expected Call Sequence */")
                mc_lines.append(f"        EXPECTED_CALLS(\"{expected_calls_str}\");")
                if any("returnValue" in l for l in pre_lines):
                    call_sut_line = f"returnValue = {call_line_plain}"
                else:
                    call_sut_line = f"{call_line_plain}"
                mc_lines.append("")
                mc_lines.append("            /* Call SUT */")
                mc_lines.append(f"            {call_sut_line}")
                mc_lines.append("")
                mc_lines.append("            /* Test case checks */")
                check_line = _emit_return_check_line(raw_ret_type, ret_check_macro, resolved_ret)
                if check_line:
                    mc_lines.append(f"            {check_line}")
                mc_lines.append("            /* Checks on global data */")
                mc_lines.append("            check_global_data();")
                mc_lines.append("        END_CALLS();")
                mc_lines.append("    END_TEST();")
                mc_lines.append("}}}")
                tests.append('\n'.join(mc_lines))
                seq += 1

    else:
        # No branches: generate tests
        # NEW: add enum sweep here, so your enum param generates multiple tests.
        for enum_idx in enum_cases:
            for sweep_idx in range(3 if sweep_params else 1):
                decl_lines, param_decl_dict = render_param_decls(sweep_idx if sweep_params else None, enum_idx, func_body)
                assign_lines = []
                exp_lines = []

                skip_assigns = pointer_param_loop_assignment_patch(
                    assign_lines, exp_lines, func_body, pointer_args, param_decl_dict
                )

                pointer_args_local = [nm for ty, nm in func.get('args', []) if '*' in ty]
                for lhs, rhs in post_assigns:
                    if skip_if_const(lhs, rhs):
                        continue
                    varname = lhs.split('.')[0].split('->')[0]
                    if is_local(varname, func_body) and not is_static_local(varname, func_body):
                        continue
                    m_arr_assign = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\[(\w+)\]', lhs)
                    if m_arr_assign:
                        arr_name, arr_idx = m_arr_assign.group(1), m_arr_assign.group(2)
                        if (arr_name, arr_idx) in skip_assigns:
                            continue

                    if any(lhs.startswith(arg + "[") for arg in pointer_args_local):
                        base = re.match(r'([A-Za-z_][A-Za-z0-9_]*)', lhs).group(1)
                        if base in [p for _, p in param_types_and_names] and base not in local_map_names:
                            btype = next((t for t, n in param_types_and_names if n == base), 'unsigned int')
                            map_var = _alloc_and_register_map(
                                base, btype.replace('*', '').strip() or 'unsigned int'
                            )
                            if map_var != "NULL":
                                local_map_names[base] = map_var
                                local_map_decls[map_var] = (
                                    btype.replace('*', '').strip() or 'unsigned int',
                                    DEFAULT_MAP_SIZE
                                )
                        assign_lines.append(f"{pointer_map_subst_local(lhs, pointer_args_local)} = {rhs};")
                        exp_lines.append(f"{expected_pointer_map_local(lhs, pointer_args_local)} = {rhs};")
                    else:
                        test_lhs, expected_lhs = make_test_names(lhs)
                        assign_lines.append(f"{test_lhs} = 0U;")
                        exp_lines.append(f"{expected_lhs} = {rhs};")

                pre_lines = decl_lines + assign_lines + exp_lines

                call_list = extract_function_calls(
                    strip_conditional_and_pragma_blocks(func_body),
                    exclude_names={name}
                )
                expected_calls_str = ";".join(f"{c}#1" for c in call_list)

                has_return = any(" returnValue" in l for l in pre_lines)
                if has_return:
                    call_sut_line = f"returnValue = {call_target}({', '.join([nm for _, nm in param_types_and_names])});"
                else:
                    call_sut_line = call_line_plain

                check_return_line = ""
                if has_return:
                    check_line = _emit_return_check_line(raw_ret_type, ret_check_macro, resolved_ret)
                    if check_line:
                        check_return_line = f"            {check_line}"

                # Keep your START_TEST format; if you want "{seq}_MC:" always, change it here.
                auto_lines = []
                auto_lines.append(f"void test_{seq}(int doIt){{")
                auto_lines.append("if (doIt) {")
                auto_lines.append("    /* Set global data */")
                auto_lines.append("    initialise_global_data();")
                auto_lines.append("    /* Set expected values for global data checks */")
                auto_lines.append("    initialise_expected_global_data();")
                auto_lines.append("    {")
                auto_lines.append("    /* Test case data declarations */")
                for _l in pre_lines:
                    auto_lines.append("    " + _l)
                auto_lines.append("")
                auto_lines.append(f"    START_TEST(\"{seq}_MC: {name}\",")
                auto_lines.append(f"               \"Created to test the function {name}\");")
                auto_lines.append("")
                auto_lines.append("        /* Expected Call Sequence */")
                auto_lines.append(f"        EXPECTED_CALLS(\"{expected_calls_str}\");")
                auto_lines.append("")
                auto_lines.append("            /* Call SUT */")
                auto_lines.append(f"            {call_sut_line}")
                auto_lines.append("")
                auto_lines.append("            /* Test case checks */")
                if check_return_line:
                    auto_lines.append(check_return_line)
                auto_lines.append("            /* Checks on global data */")
                auto_lines.append("            check_global_data();")
                auto_lines.append("        END_CALLS();")
                auto_lines.append("    END_TEST();")
                auto_lines.append("}}}")
                tests.append("\n".join(auto_lines))
                seq += 1

    header = make_cantata_header(func, c_file_path, purpose=f"Auto-generated Cantata tests for {name}.")

    file_maps = GLOBAL_FILE_MAPS.setdefault(module, {})
    for mname, info in local_map_decls.items():
        if mname not in file_maps:
            file_maps[mname] = info
        else:
            if info[1] > file_maps[mname][1]:
                file_maps[mname] = info

    return [header] + tests


# ------------------- HELPERS REFERENCED BY THE GENERATOR (MOVED VERBATIM) -------------------
# These were in your original file; generator calls them directly.

def extract_condition_selectors(func_body):
    selectors = []
    for m in re.finditer(r'\bif\s*\(\s*([A-Za-z_][\w\.->]*)\s*\)', func_body):
        selectors.append(m.group(1).strip())
    for m in re.finditer(r'\bif\s*\(\s*([A-Za-z_][\w\.->]*)\s*==\s*[A-Za-z_][\w]*\s*\)', func_body):
        selectors.append(m.group(1).strip())
    for m in re.finditer(r'\bswitch\s*\(\s*([A-Za-z_][\w\.->]*)\s*\)', func_body):
        selectors.append(m.group(1).strip())
    return list(set(selectors))


def detect_local_statics(func_body):
    body = re.sub(r'/\*.*?\*/', '', func_body, flags=re.DOTALL)
    body = re.sub(r'//.*', '', body)
    decls = re.findall(
        r'\bstatic\s+[^;{}()]*?\b([A-Za-z_]\w*)\s*(?:=\s*[^;]*)?;',
        body
    )
    seen = []
    for name in decls:
        if name not in seen:
            seen.append(name)
    return seen[:5]


def detect_parameters(func):
    return set(nm for _, nm in func.get('args', []))


def detect_locals(func_body):
    body = re.sub(r'/\*.*?\*/', '', func_body, flags=re.DOTALL)
    body = re.sub(r'//.*', '', body)
    decls = re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\s+[^\n;{]*?\b([A-Za-z_]\w*)\s*(?:=\s*[^;]*)?;', body)
    return set(name for _type, name in decls)


def filter_condition_selectors(func, func_body, selectors):
    params = detect_parameters(func)
    statics = detect_local_statics(func_body)
    locals_ = detect_locals(func_body)
    filtered = [
        sel for sel in selectors
        if sel not in params and sel not in statics and sel not in locals_
    ]
    return filtered


# ------------------- dependency stubs and wrappers (shared with GUI) -------------------

def strip_conditional_and_pragma_blocks(code: str) -> str:
    pattern = re.compile(
        r'^[ \t]*#(?:if|ifdef|ifndef)[^\n]*\n'
        r'(?:.*?\n)*?'
        r'^[ \t]*#endif[^\n]*\n?',
        flags=re.MULTILINE | re.DOTALL
    )
    while True:
        new_code, count = pattern.subn('', code)
        if count == 0:
            break
        code = new_code
    return code


def find_defined_functions(code: str) -> set:
    lines = code.splitlines()
    defined = set()
    keywords = {
        'if', 'for', 'while', 'switch', 'return', 'sizeof', 'case', 'else', 'do', 'goto',
        'alignof', 'alignas', '_Static_assert', 'break', 'continue'
    }
    buffer = ""
    name = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (
            re.match(r'^(extern\s+)?(static\s+)?(inline\s+)?[A-Za-z_][\w\s\*]*\s+[A-Za-z_]\w*\s*\(', stripped)
            or buffer
        ):
            buffer += line + "\n"
            if ')' in line:
                match = re.search(
                    r'(?:extern\s+)?(?:static\s+)?(?:inline\s+)?[A-Za-z_][\w\s\*]*\s+([A-Za-z_]\w*)\s*\([^\)]*\)',
                    buffer, re.DOTALL
                )
                if match:
                    name = match.group(1)
                buffer = buffer.strip()
                j = i + 1
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                if (j < len(lines) and lines[j].strip().startswith('{')) or stripped.endswith('{'):
                    if name and name not in keywords:
                        defined.add(name)
                buffer = ""
                name = None
        else:
            buffer = ""
            name = None
    return defined


def find_atest_file(c_file_path, project_dir):
    module = os.path.splitext(os.path.basename(c_file_path))[0]
    atest_path = os.path.join(project_dir, "atest", f"atest_{module}.c")
    return atest_path if os.path.isfile(atest_path) else None


def extract_function_signature_from_file(file_path, func_name):
    try:
        code = Path(file_path).read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return None, None
    code = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)
    code = re.sub(r'//.*', '', code)
    pattern = re.compile(
        r'((?:static\s+)?(?:inline\s+)?[A-Za-z_][\w\s\*]*?)\s+' + re.escape(func_name) + r'\s*\(([^)]*)\)'
    )
    m = pattern.search(code)
    if m:
        ret_type = m.group(1).strip()
        arg_str = m.group(2).strip()
        return ret_type, arg_str
    return None, None

def _strip_c_comments_and_strings(code: str) -> str:
    if not code:
        return ""
    code = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)
    code = re.sub(r'//.*', '', code)
    # neutralize strings/chars to reduce false hits
    code = re.sub(r'"(?:\\.|[^"])*"', '""', code)
    code = re.sub(r"'(?:\\.|[^'])*'", "''", code)
    return code


def _collect_function_prototypes_from_text(code: str) -> Dict[str, Dict[str, Any]]:
    """
    Extract prototypes from a .c/.h text.

    Returns:
      {
        "DSC_CheckFadFaults": {"return_type": "UINT8", "args_raw": "void"},
        ...
      }
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not code:
        return out

    code2 = _strip_c_comments_and_strings(code)

    # NOTE: This is intentionally conservative; it targets typical embedded prototypes.
    proto_re = re.compile(
        r'^[ \t]*'
        r'(?P<ret>(?:extern\s+)?(?:static\s+)?(?:inline\s+)?[A-Za-z_][\w\s\*]*?)'
        r'\s+'
        r'(?P<name>[A-Za-z_]\w*)'
        r'\s*\((?P<args>[^;{}]*)\)\s*;'
        r'[ \t]*$',
        re.MULTILINE
    )

    keywords = {
        "if", "else", "for", "while", "switch", "return", "do", "case", "default",
        "break", "continue", "defined", "endif", "sizeof"
    }

    for m in proto_re.finditer(code2):
        name = (m.group("name") or "").strip()
        if not name or name in keywords:
            continue
        ret = (m.group("ret") or "").strip()
        args_raw = (m.group("args") or "").strip()
        out[name] = {"return_type": ret, "args_raw": args_raw}

    return out


def _collect_function_pointer_references(code: str, candidate_names: set[str]) -> set[str]:
    """
    Finds NAME used as an rvalue in initializers/assignments:
      .cb = Foo,
      cb = Foo;
      cb = &Foo;
      cb = (Type)Foo;
    Only returns names in candidate_names (to avoid huge false positives).
    """
    out: set[str] = set()
    if not code or not candidate_names:
        return out

    code2 = _strip_c_comments_and_strings(code)

    assign_re = re.compile(
        r'=\s*'
        r'(?:\([^\)]*\)\s*)*'   # optional cast(s)
        r'(&\s*)?'
        r'([A-Za-z_]\w*)'
        r'\s*(?=,|\}|\)|;)',
        re.MULTILINE
    )

    for m in assign_re.finditer(code2):
        nm = (m.group(2) or "").strip()
        if nm in candidate_names:
            out.add(nm)

    return out


def _is_function_defined_in_text(code: str, func_name: str) -> bool:
    """
    Detects a function definition:
      Foo(...) {
    """
    if not code or not func_name:
        return False
    code2 = _strip_c_comments_and_strings(code)
    pat = re.compile(r'\b' + re.escape(func_name) + r'\s*\([^;{}]*\)\s*\{', re.MULTILINE)
    return bool(pat.search(code2))

def _default_value_for_switch_var(var_type: str) -> str:
    """
    Option A: produce a safe numeric literal for a switch default case,
    without relying on enum members (prevents IOC_ESOF_MOTOR-like issues).
    """
    t = _normalize_c_type(var_type).upper()

    # common embedded typedef buckets
    if any(x in t for x in ["UINT8", "U8", "SINT8", "S8", "CHAR", "SIGNED CHAR", "UNSIGNED CHAR"]):
        return "0xFFU"
    if any(x in t for x in ["UINT16", "U16", "SINT16", "S16", "SHORT", "UNSIGNED SHORT"]):
        return "0xFFFFU"
    if any(x in t for x in ["UINT32", "U32", "SINT32", "S32", "UNSIGNED INT", "UNSIGNED LONG", "LONG"]):
        return "0xFFFFFFFFU"
    if any(x in t for x in ["UINT64", "U64", "SINT64", "S64"]):
        return "0xFFFFFFFFFFFFFFFFU"

    # fallback: still a numeric constant (avoid enum symbols)
    return "0xFFFFFFFFU"


def _should_use_enum_value_for_switch_var(var_type: str, c_file_path: str, header_index: Dict[str, str]) -> bool:
    """
    Only use enum-member based defaults if we can confirm var_type is an enum.
    """
    clean = re.sub(r"\b(const|volatile)\b", "", (var_type or ""), flags=re.IGNORECASE)
    clean = clean.replace("*", "").strip()
    if not clean:
        return False
    try:
        include_basenames = _parse_includes_from_c_file(c_file_path)
        # If it is declared as enum somewhere, allow enum default values.
        return _is_enum_type_anywhere(clean, c_file_path, header_index or {}, include_basenames)
    except Exception:
        return False


def _is_enum_type_anywhere(
    type_name: str,
    c_file_path: str,
    header_index: Dict[str, str],
    include_basenames: List[str],
) -> bool:
    """
    Mirror of your struct/union detection, but for enum.
    """
    if not type_name:
        return False

    def _is_enum_type_name_in_text_local(tn: str, text: str) -> bool:
        if not tn or not text:
            return False
        tn2 = re.escape(tn.strip())
        pat_typedef_enum = re.compile(rf"\btypedef\s+enum\b[\s\S]*?\}}\s*{tn2}\s*;", re.MULTILINE)
        pat_named_enum = re.compile(rf"\benum\s+{tn2}\s*\{{", re.MULTILINE)
        return bool(pat_typedef_enum.search(text) or pat_named_enum.search(text))

    # 0) current .c
    try:
        if c_file_path and os.path.isfile(c_file_path):
            c_txt = Path(c_file_path).read_text(encoding="utf-8", errors="ignore")
            if _is_enum_type_name_in_text_local(type_name, c_txt):
                return True
    except Exception:
        pass

    # 1) included headers
    for base in include_basenames or []:
        hp = (header_index or {}).get(base)
        if not hp or not os.path.isfile(hp):
            continue
        try:
            txt = Path(hp).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if _is_enum_type_name_in_text_local(type_name, txt):
            return True

    # 2) fallback all headers
    for _base, hp in (header_index or {}).items():
        if not hp or not os.path.isfile(hp):
            continue
        try:
            txt = Path(hp).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if _is_enum_type_name_in_text_local(type_name, txt):
            return True

    return False

def extract_called_functions_with_signatures(
    c_file_path: str,
    project_dir: str,
    header_index: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Extract dependency functions that should be stubbed.

    NEW in this version:
    - Resolve typedef return types using header_index so types like StatusType
      become concrete (e.g., unsigned char) for stub return-value generation.
    - OS wrapper fallback: if "GetResource" prototype not found, try "Os_GetResource".
    """
    header_index = header_index or {}

    out: List[Dict[str, Any]] = []
    if not c_file_path or not os.path.isfile(c_file_path):
        return out

    # ---------------- helpers ----------------
    def _strip_c_comments_and_strings(code: str) -> str:
        if not code:
            return ""
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
        code = re.sub(r"//.*", "", code)
        code = re.sub(r'"(?:\\.|[^"])*"', '""', code)
        code = re.sub(r"'(?:\\.|[^'])*'", "''", code)
        return code

    def _strip_preprocessor_lines(code: str) -> str:
        """Remove preprocessor lines entirely (#if/#define/...) so tokens don't look like calls."""
        if not code:
            return ""
        return re.sub(r"^[ \t]*#.*$", "", code, flags=re.MULTILINE)

    def _is_noise_symbol(nm: str) -> bool:
        if not nm:
            return True
        if nm in {"defined", "__asm__", "__asm", "asm", "__volatile__", "volatile"}:
            return True
        if nm.startswith("ACCESS_FUNCTION_") or nm.startswith("ACCESS_FUNCTION"):
            return True
        if nm.startswith(("BEFORE_", "AFTER_", "REPLACE_")):
            return True
        cantata = {
            "START_TEST", "END_TEST", "EXPECTED_CALLS", "REGISTER_CALL",
            "LOG_SCRIPT_ERROR", "END_CALLS", "rule_set", "EXPORT_COVERAGE",
            "START_SCRIPT", "END_SCRIPT", "OPEN_LOG",
            "IF_INSTANCE",
        }
        return nm in cantata

    def _collect_function_prototypes_from_text(code: str) -> Dict[str, Dict[str, Any]]:
        outp: Dict[str, Dict[str, Any]] = {}
        if not code:
            return outp

        code2 = _strip_c_comments_and_strings(code)
        code2 = _strip_preprocessor_lines(code2)

        proto_re = re.compile(
            r"^[ \t]*"
            r"(?P<ret>(?:extern\s+)?(?:static\s+)?(?:inline\s+)?[A-Za-z_][\w\s\*]*?)"
            r"\s+"
            r"(?P<name>[A-Za-z_]\w*)"
            r"\s*\((?P<args>[^;{}]*)\)\s*;"
            r"[ \t]*$",
            re.MULTILINE,
        )

        keywords = {
            "if", "else", "for", "while", "switch", "return", "do", "case", "default",
            "break", "continue", "defined", "endif", "sizeof",
            "struct", "union", "enum", "typedef",
        }

        for m in proto_re.finditer(code2):
            name = (m.group("name") or "").strip()
            if not name or name in keywords or _is_noise_symbol(name):
                continue
            ret = (m.group("ret") or "").strip()
            args_raw = (m.group("args") or "").strip()
            outp[name] = {"return_type": ret, "args_raw": args_raw}
        return outp

    def _collect_normal_calls(code: str) -> set[str]:
        code2 = _strip_c_comments_and_strings(code)
        code2 = _strip_preprocessor_lines(code2)
        call_re = re.compile(r"\b([A-Za-z_]\w*)\s*\(", re.MULTILINE)

        blacklist = {
            "if", "for", "while", "switch", "return", "sizeof", "case", "else", "do",
            "TASK", "FUNC", "P2VAR", "P2CONST", "P2FUNC", "P2P2VAR", "P2P2CONST",
            "defined", "__asm__", "__asm", "asm", "volatile",
        }

        outc: set[str] = set()
        for m in call_re.finditer(code2):
            nm = (m.group(1) or "").strip()
            if not nm or nm in blacklist:
                continue
            if nm.isupper():
                continue
            if _is_noise_symbol(nm):
                continue
            outc.add(nm)
        return outc

    def _collect_function_pointer_references(code: str, proto_db: Dict[str, Dict[str, Any]]) -> set[str]:
        code2 = _strip_c_comments_and_strings(code)
        code2 = _strip_preprocessor_lines(code2)

        assign_re = re.compile(
            r"(?P<lhs>[^=\n;{}]+?)"
            r"=\s*"
            r"(?:\([^\)]*\)\s*)*"
            r"(?:&\s*)?"
            r"(?P<name>[A-Za-z_]\w*)"
            r"(?=\s*(?:,|\}|\)|;))",
            re.MULTILINE,
        )

        keywords = {
            "if", "else", "for", "while", "switch", "return", "do", "case", "default",
            "break", "continue", "defined", "endif", "sizeof",
            "volatile", "static", "extern", "inline", "const", "restrict",
            "struct", "union", "enum", "typedef",
            "true", "false", "TRUE", "FALSE",
        }

        outfp: set[str] = set()
        for m in assign_re.finditer(code2):
            lhs = (m.group("lhs") or "").strip()
            nm = (m.group("name") or "").strip()

            if not nm or nm in keywords:
                continue
            if nm.isupper():
                continue
            if _is_noise_symbol(nm):
                continue

            lhs_is_designated = "." in lhs
            lhs_is_fp_deref = "(*" in lhs or "*)" in lhs
            if not (lhs_is_designated or lhs_is_fp_deref):
                continue

            if proto_db is not None and nm not in proto_db:
                continue

            outfp.add(nm)

        return outfp

    def _is_function_defined_in_text(code: str, func_name: str) -> bool:
        if not code or not func_name:
            return False
        code2 = _strip_c_comments_and_strings(code)
        code2 = _strip_preprocessor_lines(code2)
        pat = re.compile(r"\b" + re.escape(func_name) + r"\s*\([^;{}]*\)\s*\{", re.MULTILINE)
        return bool(pat.search(code2))

    def _split_args_preserving_parens(args_raw: str) -> List[str]:
        args_raw = (args_raw or "").strip()
        if not args_raw or args_raw == "void":
            return []
        out_parts: List[str] = []
        buf: List[str] = []
        depth = 0
        for ch in args_raw:
            if ch == "," and depth == 0:
                part = "".join(buf).strip()
                if part:
                    out_parts.append(part)
                buf = []
                continue
            buf.append(ch)
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
        last = "".join(buf).strip()
        if last:
            out_parts.append(last)
        return out_parts

    def _args_raw_to_list(args_raw: str) -> List[Tuple[str, str]]:
        args_raw = (args_raw or "").strip()
        if not args_raw or args_raw == "void":
            return []
        parts = _split_args_preserving_parens(args_raw)
        outa: List[Tuple[str, str]] = []
        for idx, p in enumerate(parts, start=1):
            p = p.split("=", 1)[0].strip()
            if not p:
                continue

            m_fp = re.search(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)", p)
            if m_fp:
                outa.append((p, m_fp.group(1)))
                continue

            m_arr = re.match(r"^(?P<t>.+?)\s+(?P<n>[A-Za-z_]\w*)\s*(?P<arr>\[[^\]]*\]\s*)+$", p)
            if m_arr:
                t = (m_arr.group("t") or "").strip() + " " + (m_arr.group("arr") or "").strip()
                n = (m_arr.group("n") or "").strip()
                outa.append((t.strip(), n))
                continue

            m = re.match(r"^(?P<t>.+?)\s+(?P<n>[A-Za-z_]\w*)$", p)
            if m:
                outa.append(((m.group("t") or "").strip(), (m.group("n") or "").strip()))
            else:
                outa.append((p, f"p{idx}"))
        return outa

    # -------- read module .c --------
    try:
        c_text = Path(c_file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return out

    # -------- read atest (signatures only) --------
    atest_text = ""
    atest_path = None
    try:
        atest_path = find_atest_file_in_workspace(project_dir, c_file_path)
        if atest_path and os.path.isfile(atest_path):
            atest_text = Path(atest_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        atest_text = ""

    # -------- prototype DB (module + atest only) --------
    proto_db: Dict[str, Dict[str, Any]] = {}
    proto_db.update(_collect_function_prototypes_from_text(c_text))
    if atest_text:
        proto_db.update(_collect_function_prototypes_from_text(atest_text))

    # -------- candidates --------
    candidates: set[str] = set()
    candidates |= _collect_normal_calls(c_text)
    candidates |= _collect_function_pointer_references(c_text, proto_db)
    if atest_text:
        candidates |= _collect_function_pointer_references(atest_text, proto_db)

    # exclude anything defined in module .c
    candidates = {c for c in candidates if not _is_function_defined_in_text(c_text, c)}
    candidates = {c for c in candidates if not _is_noise_symbol(c)}

    # -------- OS prefix aliasing for signature lookup --------
    def _resolve_os_alias(nm: str) -> str:
        if not nm or nm.startswith("Os_"):
            return nm
        if nm in proto_db:
            return nm
        os_nm = "Os_" + nm
        if os_nm in proto_db:
            return os_nm
        return nm

    # -------- NEW: typedef DB for resolving return types like StatusType --------
    include_basenames = _parse_includes_from_c_file(c_file_path)
    typedef_db = _build_typedef_db(header_index, include_basenames)

    def _resolve_return_type(ret_type: str) -> str:
        """
        ret_type might be 'StatusType' or 'FLAG' etc.
        We resolve typedef chains *only if it's not a pointer*.
        """
        rt = (ret_type or "").strip()
        if not rt:
            return ""
        if _is_pointer_type(rt):
            return rt
        resolved = _resolve_typedef_chain(rt, typedef_db)
        return resolved or rt

    # -------- build output --------
    final_names: set[str] = set()
    for nm in candidates:
        final_names.add(_resolve_os_alias(nm))

    for name in sorted(final_names):
        sig = proto_db.get(name, {})
        ret_type = (sig.get("return_type") or "void").strip() or "void"
        args_raw = sig.get("args_raw")
        args_raw = "" if args_raw is None else str(args_raw).strip()

        resolved_ret = _resolve_return_type(ret_type)

        out.append({
            "name": name,
            "return_type": ret_type,
            "return_type_resolved": resolved_ret,  # <-- NEW: for StatusType -> unsigned char
            "args": _args_raw_to_list(args_raw),
            "args_str": args_raw,
            "source_c_path": c_file_path,          # <-- handy for debugging
            "atest_c_path": atest_path or "",      # <-- handy for debugging
        })

    return out

def render_stub(
    funcsig: dict,
    c_file_path: str = "",
    project_dir: str = "",
    header_index: Optional[Dict[str, str]] = None,
) -> str:
    """
    Render a Cantata stub for a function signature.

    Expected funcsig keys (best effort):
      - name: str
      - return_type: str
      - args_str: Optional[str]  (raw prototype args: "", "void", or "T a, U8 b")
      - args: Optional[List[Tuple[str,str]]] (parsed args)
    """
    header_index = header_index or {}
    name = funcsig["name"]
    rtype = (funcsig.get("return_type") or "void").strip()

    # Prefer args_str if present because it preserves () vs (void)
    args_str = funcsig.get("args_str", None)
    if args_str is not None:
        args_str = str(args_str).strip()
    else:
        args_str = ""  # means "unknown from raw"; we'll build from args list

    # ---- parse args_str into [(type, name), ...] ----
    def _split_args(arglist: str) -> List[str]:
        parts: List[str] = []
        depth = 0
        buf = ""
        for ch in (arglist or ""):
            if ch == "," and depth == 0:
                if buf.strip():
                    parts.append(buf.strip())
                buf = ""
                continue
            buf += ch
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
        if buf.strip():
            parts.append(buf.strip())
        return parts

    def _parse_one_arg(a: str, idx: int) -> Tuple[str, str]:
        a = (a or "").strip()
        if not a or a == "void":
            return ("", "")

        a2 = re.sub(r"\s+", " ", a).strip()
        m = re.match(r"^(?P<t>.+?)\s+(?P<n>[A-Za-z_]\w*)$", a2)
        if m:
            return (m.group("t").strip(), m.group("n").strip())
        return (a2, f"p{idx}")

    # Build args_list from args_str (if present) else from structured args
    args_list: List[Tuple[str, str]] = []
    if args_str and args_str != "void":
        raw_args = _split_args(args_str)
        for i, a in enumerate(raw_args, start=1):
            t, n = _parse_one_arg(a, i)
            if t and n:
                args_list.append((t, n))
    else:
        # fallback to structured args if present
        maybe_args = funcsig.get("args") or []
        if isinstance(maybe_args, list):
            for i, a in enumerate(maybe_args, start=1):
                if isinstance(a, (list, tuple)) and len(a) == 2:
                    t = (a[0] or "").strip()
                    n = (a[1] or "").strip() or f"p{i}"
                    if t:
                        args_list.append((t, n))

    # -------- type resolving for stub values --------
    def _strip_qualifiers(t: str) -> str:
        t = (t or "").strip()
        t = re.sub(r"\b(const|volatile|static|inline|extern|register|restrict)\b", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    def _map_resolved_to_value_group(resolved: str) -> Optional[str]:
        """
        Convert resolved C base type into one of your value groups
        so test_values_for_type() can emit good values.
        """
        r = _normalize_c_type(resolved).lower()

        # unsigned
        if r in ("unsigned char",):
            return "U8"
        if r in ("unsigned short", "unsigned short int"):
            return "U16"
        if r in ("unsigned", "unsigned int"):
            return "U32"
        if r in ("unsigned long", "unsigned long int"):
            # depends on platform, but U32 is usually fine for embedded
            return "U32"
        if r in ("unsigned long long", "unsigned long long int"):
            return "U64"

        # signed
        if r in ("char", "signed char"):
            return "S8"
        if r in ("short", "short int", "signed short", "signed short int"):
            return "S16"
        if r in ("int", "signed", "signed int"):
            return "S32"
        if r in ("long", "long int", "signed long", "signed long int"):
            return "S32"
        if r in ("long long", "long long int", "signed long long", "signed long long int"):
            return "S64"

        return None

    def _resolve_type_for_stub(type_str: str) -> str:
        """
        Best-effort type normalization for generating stub locals and values.
        Goal: make StatusType -> underlying (U8/U16/...) when possible.
        """
        ty = _strip_qualifiers(type_str)
        if not ty:
            return type_str

        # Keep pointers as-is (we typically don't want to synthesize scalar values for them)
        if "*" in ty:
            return ty

        # Keep function pointers as-is
        if "(*" in ty:
            return ty

        # If it already matches one of your known typedef patterns, keep it
        if get_typename_group(ty) is not None:
            return ty

        # Try to resolve typedef chains using headers included by this module .c
        try:
            include_bases = _parse_includes_from_c_file(c_file_path) if c_file_path else []
            typedef_db = _build_typedef_db(header_index or {}, include_bases)
            resolved = _resolve_typedef_chain(ty, typedef_db) if typedef_db else ""
        except Exception:
            resolved = ""

        if resolved:
            grp = _map_resolved_to_value_group(resolved)
            if grp:
                # Return the group string so test_values_for_type() works well
                return grp
            # If it's not mappable, still return resolved text (helps signature readability)
            return resolved

        # Fallback: return original
        return ty

    # --------- signature argument rendering ---------
    # Priority:
    # 1) If args_str is explicitly provided:
    #       ""     -> ()
    #       "void" -> (void)
    #       else   -> (args_str)
    # 2) Else (args_str not provided originally):
    #       if args_list exists -> (typed args)
    #       else -> ()   (do NOT force void)
    if "args_str" in funcsig:
        if args_str == "":
            sig_args = ""
        elif args_str == "void":
            sig_args = "void"
        else:
            sig_args = args_str
    else:
        if args_list:
            sig_args = ", ".join([f"{t} {n}".strip() for (t, n) in args_list])
        else:
            sig_args = ""  # keep empty, not void

    signature = f"{rtype} {name}({sig_args})"
    _rtype_base = re.sub(
        r"\b(static|extern|inline|const|volatile|register|restrict)\b",
        "",
        rtype,
        flags=re.IGNORECASE,
    )
    _rtype_base = re.sub(r"\s+", " ", _rtype_base).strip().lower()
    needs_ret = (_rtype_base != "void")

    out: List[str] = []
    out.append(f"/* Stub for function {name} */")
    out.append(signature)
    out.append("{")

    # ---- declare returnValue BEFORE REGISTER_CALL ----
    if needs_ret:
        out.append(f"    {rtype} returnValue;")

    out.append(f"    REGISTER_CALL(\"{name}\");")
    out.append("")

    # ---- return handling ----
    if needs_ret:
        rtype_resolved = _resolve_type_for_stub(rtype)
        values = test_values_for_type(rtype_resolved) or test_values_for_type(rtype)
        if not values:
            if "float" in rtype.lower():
                values = ["0.0", "1.234", "9999.9"]
            elif "double" in rtype.lower():
                values = ["0.0", "1.234", "9999.9"]
            elif "char" in rtype.lower():
                values = ["'\\0'", "'A'", "'\\x7F'"]
            else:
                values = ["0", "1", "0xFFFFFFFF"]

        out.append('    IF_INSTANCE("1") {')
        out.append(f"        returnValue = {values[0]};")
        out.append("        return returnValue;")
        out.append("    }")
        out.append("")
        out.append('    IF_INSTANCE("2") {')
        out.append(f"        returnValue = {values[1]};")
        out.append("        return returnValue;")
        out.append("    }")
        out.append("")
        out.append('    IF_INSTANCE("3") {')
        out.append(f"        returnValue = {values[2]};")
        out.append("        return returnValue;")
        out.append("    }")
    else:
        out.append('    IF_INSTANCE("1") {')
        out.append("        return;")
        out.append("    }")

    out.append("")
    out.append('    LOG_SCRIPT_ERROR("Call instance not defined.");')
    if needs_ret:
        out.append("    return returnValue;")
    else:
        out.append("    return;")
    out.append("}")
    return "\n".join(out)

def _append_unique(lines: List[str], line: str) -> None:
    """Append a line only if it's not already present (prevents duplicates)."""
    if line not in lines:
        lines.append(line)

def generate_wrapper(func):
    name = func.get('name')
    ret_type = (func.get('return_type') or 'void').strip()
    args_tup = func.get('args', [])
    args_list = []
    for t, n in args_tup:
        t = (t or '').strip()
        n = (n or '').strip()
        if n:
            args_list.append(f"{t} {n}".strip())
        else:
            args_list.append(t)
    args = ", ".join(args_list) if args_list else "void"

    before_lines = [f"/* Before-Wrapper for function {name} */",
                    f"int BEFORE_{name}({args})" + "{",
                    f"    REGISTER_CALL(\"{name}\");",
                    ""]
    if ret_type.lower() == "void":
        before_lines.append(f"    IF_INSTANCE(\"1\") {{")
        if args_tup and args_tup[0][1]:
            first_param = args_tup[0][1]
            before_lines.append(f"        CHECK_S_INT({first_param}, CHD_NORMAL_OPERATION);")
        before_lines.append(f"        return REPLACE_WRAPPER;")
        before_lines.append(f"    }}")
    else:
        for idx in range(1, 4):
            before_lines.append(f"    IF_INSTANCE(\"{idx}\") {{")
            before_lines.append(f"        return REPLACE_WRAPPER;")
            before_lines.append(f"    }}")
    before_lines.append("")
    before_lines.append(f"    LOG_SCRIPT_ERROR(\"Call instance not defined.\");")
    before_lines.append(f"    return AFTER_WRAPPER;")
    before_lines.append("}")
    before_lines.append("")

    if args != "void":
        after_args = f"struct cppsm_void_return cppsm_dummy, {args}" if ret_type.lower() == "void" else f"{ret_type} cppsm_return_value, {args}"
    else:
        after_args = f"struct cppsm_void_return cppsm_dummy" if ret_type.lower() == "void" else f"{ret_type} cppsm_return_value"
    after_lines = [
        f"/* After-Wrapper for function {name} */",
        f"{ret_type} AFTER_{name}({after_args})" + "{"
    ]
    if ret_type.lower() != "void":
        after_lines.append(f"    {ret_type} returnValue;")
    after_lines.append(f"    LOG_SCRIPT_ERROR(\"Call instance not defined.\");")
    after_lines.append(f"    return;" if ret_type.lower() == "void" else f"    return cppsm_return_value;")
    after_lines.append("}")
    after_lines.append("")

    replace_lines = [f"/* Replace-Wrapper for function {name} */",
                     f"{ret_type} REPLACE_{name}({args})" + "{"]

    if ret_type.lower() != "void":
        replace_lines.append(f"    {ret_type} returnValue;")
        values = test_values_for_type(ret_type)
        if not values:
            if "float" in ret_type:
                values = ["0.0", "1.234", "9999.9"]
            elif "char" in ret_type.lower():
                values = ["'\\0'", "'A'", "'\\x7F'"]
            else:
                values = ["0", "1", "0xFFFFFFFF"]
        for idx, val in enumerate(values, start=1):
            replace_lines.append(f"    IF_INSTANCE(\"{idx}\") {{")
            replace_lines.append(f"        returnValue = {val};")
            replace_lines.append(f"        return returnValue;")
            replace_lines.append(f"    }}")
        replace_lines.append("")
        replace_lines.append("    LOG_SCRIPT_ERROR(\"Call instance not defined.\");")
        replace_lines.append("    return returnValue;")
    else:
        replace_lines.append(f"    IF_INSTANCE(\"1\") {{")
        replace_lines.append(f"        return;")
        replace_lines.append(f"    }}")
        replace_lines.append("")
        replace_lines.append("    LOG_SCRIPT_ERROR(\"Call instance not defined.\");")
        replace_lines.append("    return;")
    replace_lines.append("}")
    replace_lines.append("")

    return "\n".join(before_lines + after_lines + replace_lines)


def find_wrapped_functions(file_path, functions):
    # Map name -> function dict
    defined_funcs = {f['name']: f for f in functions if 'name' in f and f['name']}
    called_in_other = set()
    func_bodies = {}

    # Read code (no comments)
    try:
        code = Path(file_path).read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return []

    # Get function bodies
    for f in functions:
        # FIX: use the imported name get_function_source (not get_function_source)
        func_body, _ = get_function_source(file_path, f['name'])
        func_bodies[f['name']] = func_body

    # For each function, see which other functions it calls
    for fn, body in func_bodies.items():
        for cname in defined_funcs:
            if cname == fn:
                continue  # skip self
            if re.search(r'(?<![A-Za-z0-9_])' + re.escape(cname) + r'\s*\(', body):
                called_in_other.add(cname)

    # Only generate wrappers for functions called in other functions
    need_wrappers = [defined_funcs[f] for f in called_in_other if f in defined_funcs]

    # Preserve order as in original functions list
    names = set()
    ordered = []
    wanted = {fw['name'] for fw in need_wrappers}
    for f in functions:
        if f.get('name') in wanted and f.get('name') not in names:
            ordered.append(f)
            names.add(f.get('name'))
    return ordered