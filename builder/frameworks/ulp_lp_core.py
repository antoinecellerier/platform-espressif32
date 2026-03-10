# Copyright 2020-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from SCons.Script import Import, Return

Import("env")

# Skip when the CMake-based ulp.py handles ULP compilation
if "espidf" in env.subst("$PIOFRAMEWORK"):
    Return()

platform = env.PioPlatform()
board = env.BoardConfig()
mcu = board.get("build.mcu", "esp32")

#
# Per-MCU LP-Core configuration
#

LP_CORE_MCUS = {
    "esp32c5": {
        "peripherals_ld": "esp32c5.peripherals.ld",
        "has_lp_rom": False,
        "has_touch": False,
    },
    "esp32c6": {
        "peripherals_ld": "esp32c6.peripherals.ld",
        "has_lp_rom": False,
        "has_touch": False,
    },
    "esp32p4": {
        "peripherals_ld": "esp32p4.peripherals.ld",
        "has_lp_rom": True,
        "has_touch": True,
    },
}

if mcu not in LP_CORE_MCUS:
    Return()

mcu_config = LP_CORE_MCUS[mcu]

#
# Path resolution
#

PROJECT_DIR = Path(env.subst("$PROJECT_DIR"))
BUILD_DIR = Path(env.subst("$BUILD_DIR"))
ULP_DIR = PROJECT_DIR / "ulp"

if not ULP_DIR.is_dir() or not any(
    f.suffix in (".c", ".S", ".s") for f in ULP_DIR.iterdir() if f.is_file()
):
    Return()

ULP_BUILD_DIR = BUILD_DIR / "ulp_lp_core"
ULP_GEN_DIR = ULP_BUILD_DIR / "generated"

_toolchain_dir = platform.get_package_dir("toolchain-riscv32-esp")
if not _toolchain_dir or not os.path.isdir(_toolchain_dir):
    sys.stderr.write(
        "Error: toolchain-riscv32-esp not found. Required for LP-Core ULP builds.\n"
        "  Install with: pio pkg install -g -p espressif32 -t toolchain-riscv32-esp\n"
    )
    env.Exit(1)

TOOLCHAIN_DIR = Path(_toolchain_dir)
GCC = TOOLCHAIN_DIR / "bin" / "riscv32-esp-elf-gcc"
OBJCOPY = TOOLCHAIN_DIR / "bin" / "riscv32-esp-elf-objcopy"
READELF = TOOLCHAIN_DIR / "bin" / "riscv32-esp-elf-readelf"

if not GCC.exists():
    sys.stderr.write("Error: RISC-V GCC not found at %s\n" % GCC)
    env.Exit(1)

_fw_libs_dir = platform.get_package_dir("framework-arduinoespressif32-libs")
if not _fw_libs_dir or not os.path.isdir(_fw_libs_dir):
    sys.stderr.write("Error: framework-arduinoespressif32-libs not found.\n")
    env.Exit(1)

FW_LIBS_DIR = Path(_fw_libs_dir)
FW_LIBS = FW_LIBS_DIR / mcu / "include"

if not FW_LIBS.exists():
    sys.stderr.write(
        "Error: framework-arduinoespressif32-libs headers not found at %s\n" % FW_LIBS
    )
    env.Exit(1)

_espidf_dir = platform.get_package_dir("framework-espidf")
if not _espidf_dir or not os.path.isdir(_espidf_dir):
    sys.stderr.write(
        "Error: framework-espidf not found. Required for LP-Core ULP builds.\n"
        "  Install with: pio pkg install -g -p espressif32 -t framework-espidf\n"
    )
    env.Exit(1)

IDF_DIR = Path(_espidf_dir)
IDF_COMPONENTS = IDF_DIR / "components"
LP_CORE_DIR = IDF_COMPONENTS / "ulp" / "lp_core" / "lp_core"
LP_SHARED_DIR = IDF_COMPONENTS / "ulp" / "lp_core" / "shared"

#
# sdkconfig.h — use project-provided or auto-generate
#
# Note: custom_sdkconfig values specified via file:// or http:// URLs are not
# resolved here. Provide ulp/sdkconfig.h in the project for full control.
#

USER_SDKCONFIG_H = ULP_DIR / "sdkconfig.h"
GENERATED_SDKCONFIG_H = ULP_BUILD_DIR / "sdkconfig.h"


def get_sdkconfig_value(key, default):
    try:
        custom = env.GetProjectOption("custom_sdkconfig", "")
        for line in custom.splitlines():
            line = line.strip()
            if "://" in line:
                continue
            if line.startswith(key + "="):
                return int(line.split("=", 1)[1])
    except Exception:
        pass
    return default


ULP_RESERVE_MEM = get_sdkconfig_value("CONFIG_ULP_COPROC_RESERVE_MEM", 8192)
ULP_SHARED_MEM = get_sdkconfig_value("CONFIG_ULP_SHARED_MEM", 16)


def generate_sdkconfig_h():
    ULP_BUILD_DIR.mkdir(parents=True, exist_ok=True)

    mcu_upper = mcu.upper()
    lines = [
        "/* Auto-generated sdkconfig.h for LP-Core ULP compilation */",
        "#pragma once",
        "",
        "#define CONFIG_IDF_TARGET_%s 1" % mcu_upper,
        "#define CONFIG_ULP_COPROC_ENABLED 1",
        "#define CONFIG_ULP_COPROC_TYPE_LP_CORE 1",
        "#define CONFIG_ULP_COPROC_RESERVE_MEM %d" % ULP_RESERVE_MEM,
        "#define CONFIG_ULP_SHARED_MEM %d" % ULP_SHARED_MEM,
        "#define CONFIG_LOG_DEFAULT_LEVEL 0",
        "#define CONFIG_LOG_MAXIMUM_LEVEL 0",
    ]

    if mcu_config.get("has_lp_rom"):
        lines.append("#define CONFIG_ESP_ROM_HAS_LP_ROM 1")

    lines.append("")
    content = "\n".join(lines)

    # Only write if changed to avoid unnecessary rebuilds
    if GENERATED_SDKCONFIG_H.exists() and GENERATED_SDKCONFIG_H.read_text() == content:
        return
    GENERATED_SDKCONFIG_H.write_text(content)


if USER_SDKCONFIG_H.exists():
    SDKCONFIG_H = USER_SDKCONFIG_H
else:
    generate_sdkconfig_h()
    SDKCONFIG_H = GENERATED_SDKCONFIG_H

#
# Source files
#


def collect_ulp_sources():
    return sorted(
        f for f in ULP_DIR.iterdir()
        if f.is_file() and f.suffix in (".c", ".S", ".s")
    )


def collect_idf_sources():
    sources = [
        LP_CORE_DIR / "start.S",
        LP_CORE_DIR / "vector.S",
        LP_CORE_DIR / "port" / mcu / "vector_table.S",
        LP_CORE_DIR / "lp_core_startup.c",
        LP_CORE_DIR / "lp_core_utils.c",
        LP_CORE_DIR / "lp_core_i2c.c",
        LP_CORE_DIR / "lp_core_interrupt.c",
        LP_CORE_DIR / "lp_core_panic.c",
        LP_CORE_DIR / "lp_core_print.c",
        LP_CORE_DIR / "lp_core_uart.c",
        LP_CORE_DIR / "lp_core_ubsan.c",
        LP_CORE_DIR / "lp_core_spi.c",
        IDF_COMPONENTS / "hal" / "uart_hal_iram.c",
        IDF_COMPONENTS / "hal" / "uart_hal.c",
        LP_SHARED_DIR / "ulp_lp_core_memory_shared.c",
        LP_SHARED_DIR / "ulp_lp_core_lp_timer_shared.c",
        LP_SHARED_DIR / "ulp_lp_core_lp_uart_shared.c",
        LP_SHARED_DIR / "ulp_lp_core_critical_section_shared.c",
        LP_SHARED_DIR / "ulp_lp_core_lp_adc_shared.c",
        LP_SHARED_DIR / "ulp_lp_core_lp_vad_shared.c",
    ]

    touch_src = LP_CORE_DIR / "lp_core_touch.c"
    if mcu_config.get("has_touch") and touch_src.exists():
        sources.append(touch_src)

    return [s for s in sources if s.exists()]


#
# Include directories
#

INCLUDES = [
    ULP_DIR,
    ULP_BUILD_DIR,
    IDF_COMPONENTS / "ulp" / "lp_core" / "include",
    IDF_COMPONENTS / "ulp" / "ulp_common" / "include",
    LP_CORE_DIR / "include",
    LP_SHARED_DIR / "include",
    IDF_COMPONENTS / "esp_system" / "ld",
    FW_LIBS / "riscv" / "include",
    FW_LIBS / "soc" / mcu / "include",
    FW_LIBS / "soc" / mcu / "register",
    FW_LIBS / "soc" / "include",
    FW_LIBS / "hal" / "include",
    FW_LIBS / "hal" / mcu / "include",
    FW_LIBS / "hal" / "platform_port" / "include",
    FW_LIBS / "esp_common" / "include",
    FW_LIBS / "esp_rom" / "include",
    FW_LIBS / "esp_rom" / mcu,
    FW_LIBS / "esp_rom" / mcu / "include",
    FW_LIBS / "esp_rom" / mcu / "include" / mcu,
    FW_LIBS / "esp_system" / "port" / "soc",
    FW_LIBS / "esp_hw_support" / "include",
    FW_LIBS / "esp_hw_support" / "include" / "soc",
    FW_LIBS / "esp_hw_support" / "include" / "soc" / mcu,
    FW_LIBS / "esp_hw_support" / "port" / mcu,
    FW_LIBS / "esp_hw_support" / "port" / mcu / "include",
    # newlib/platform_include intentionally excluded —
    # pioarduino's newlib wrapper conflicts with LP-Core bare-metal compilation
    FW_LIBS / "log" / "include",
    FW_LIBS / "esp_timer" / "include",
    FW_LIBS / "esp_driver_uart" / "include",
    FW_LIBS / "heap" / "include",
]

INCLUDES = [inc for inc in INCLUDES if inc.exists()]

#
# Compiler and linker flags (matching toolchain-lp-core-riscv.cmake)
#

CFLAGS = [
    "-include", str(SDKCONFIG_H),
    "-Os", "-ggdb",
    "-march=rv32imac_zicsr_zifencei",
    "-mdiv",
    "-fdata-sections", "-ffunction-sections",
    "-fno-builtin",
    "-DIS_ULP_COCPU",
]

ASFLAGS = [
    "-include", str(SDKCONFIG_H),
    "-march=rv32imac_zicsr_zifencei",
    "-x", "assembler-with-cpp",
    "-DIS_ULP_COCPU",
]

LDFLAGS = [
    "-march=rv32imac_zicsr_zifencei",
    "-nostartfiles",
    "-Wl,--gc-sections",
    "-Wl,--no-warn-rwx-segments",
    "--specs=nano.specs",
    "--specs=nosys.specs",
]

LP_LD_TEMPLATE = IDF_COMPONENTS / "ulp" / "ld" / "lp_core_riscv.ld"
PERIPHERALS_LD = IDF_COMPONENTS / "soc" / mcu / "ld" / mcu_config["peripherals_ld"]

#
# Output files
#

LP_ELF = ULP_BUILD_DIR / "ulp_main.elf"
LP_BIN = ULP_BUILD_DIR / "ulp_main.bin"
LP_SYM = ULP_BUILD_DIR / "ulp_main.sym"
LP_MAP = ULP_BUILD_DIR / "ulp_main.map"
LP_LD_PROCESSED = ULP_BUILD_DIR / "lp_core_riscv.ld"
LP_MAPGEN_LD = ULP_BUILD_DIR / "ulp_main.ld"
LP_HEADER = ULP_GEN_DIR / "ulp_main.h"
LP_BIN_HEADER = ULP_GEN_DIR / "ulp_main_bin.h"
LP_BIN_ASM = ULP_BUILD_DIR / "ulp_main.bin.S"

MAPGEN_SCRIPT = IDF_COMPONENTS / "ulp" / "esp32ulp_mapgen.py"


def get_include_flags():
    flags = []
    for inc in INCLUDES:
        flags.extend(["-I", str(inc)])
    return flags


def run_cmd(cmd, error_msg):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write("%s:\n%s\n" % (error_msg, result.stderr))
        env.Exit(1)
    return result


def obj_name_for(src):
    # Derive unique object name to prevent collisions between user and IDF sources
    try:
        rel = src.relative_to(IDF_COMPONENTS)
        prefix = "idf_" + str(rel.parent).replace(os.sep, "_") + "_"
    except ValueError:
        try:
            rel = src.relative_to(ULP_DIR)
            prefix = "app_"
        except ValueError:
            prefix = "ext_"
    return prefix + src.stem + ".o"


def preprocess_linker_script():
    ULP_BUILD_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(GCC),
        "-include", str(SDKCONFIG_H),
        "-D__ASSEMBLER__", "-E", "-P", "-xc",
    ] + get_include_flags() + [
        "-o", str(LP_LD_PROCESSED),
        str(LP_LD_TEMPLATE),
    ]
    run_cmd(cmd, "Failed to preprocess LP-Core linker script")


def compile_source(src):
    obj = ULP_BUILD_DIR / obj_name_for(src)
    is_asm = src.suffix in (".S", ".s")
    cmd = [str(GCC)]
    cmd.extend(ASFLAGS if is_asm else CFLAGS)
    cmd.extend(get_include_flags())
    cmd.extend(["-c", str(src), "-o", str(obj)])
    run_cmd(cmd, "Failed to compile %s" % src.name)
    return obj


def link_elf(objects):
    cmd = [str(GCC)]
    cmd.extend(LDFLAGS)
    cmd.extend(["-Wl,-Map=%s" % LP_MAP])
    cmd.extend(["-T", str(LP_LD_PROCESSED)])
    cmd.extend(["-T", str(PERIPHERALS_LD)])
    cmd.extend([str(o) for o in objects])
    cmd.extend(["-o", str(LP_ELF)])
    run_cmd(cmd, "Failed to link LP-Core ELF")


def generate_binary():
    cmd = [str(OBJCOPY), "-O", "binary", str(LP_ELF), str(LP_BIN)]
    run_cmd(cmd, "Failed to generate LP-Core binary")


def generate_symbol_header():
    result = run_cmd(
        [str(READELF), "-sW", str(LP_ELF)],
        "Failed to read LP-Core ELF symbols",
    )
    LP_SYM.write_text(result.stdout)

    mapgen_output = ULP_BUILD_DIR / "ulp_main"
    run_cmd(
        [
            sys.executable, str(MAPGEN_SCRIPT),
            "-s", str(LP_SYM),
            "-o", str(mapgen_output),
            "--base-addr", "0x0",
            "-p", "ulp_",
        ],
        "Failed to run esp32ulp_mapgen",
    )

    shutil.copy2(str(mapgen_output) + ".h", str(LP_HEADER))


def generate_binary_header():
    lines = [
        "/* Auto-generated LP-Core binary declarations — do not edit. */",
        "#pragma once",
        "#include <stdint.h>",
        "#include <stddef.h>",
        "",
        "extern const uint8_t ulp_main_bin[];",
        "extern const uint8_t _binary_ulp_main_bin_start[];",
        "extern const uint8_t _binary_ulp_main_bin_end[];",
        "extern const unsigned long ulp_main_bin_length;",
        "",
    ]
    LP_BIN_HEADER.write_text("\n".join(lines))


def generate_binary_assembly():
    # Replicate ESP-IDF's data_file_embed_asm.cmake in Python
    bin_data = LP_BIN.read_bytes()
    bin_len = len(bin_data)

    lines = [
        "/*",
        " * Data converted from %s" % LP_BIN.name,
        " * Generated by ulp_lp_core.py",
        " */",
        ".data",
        "#if !defined (__APPLE__) && !defined (__linux__)",
        ".section .rodata.embedded",
        "#endif",
        "",
        ".global ulp_main_bin",
        "ulp_main_bin:",
        "",
        ".global _binary_ulp_main_bin_start",
        "_binary_ulp_main_bin_start:",
    ]

    for i in range(0, bin_len, 16):
        chunk = bin_data[i : i + 16]
        hex_str = ", ".join("0x%02x" % b for b in chunk)
        lines.append(".byte %s" % hex_str)

    lines.extend([
        "",
        ".global _binary_ulp_main_bin_end",
        "_binary_ulp_main_bin_end:",
        "",
        ".global ulp_main_bin_length",
        "ulp_main_bin_length:",
        ".long %d" % bin_len,
        "",
        "#if defined (__linux__)",
        '.section .note.GNU-stack,"",@progbits',
        "#endif",
    ])

    LP_BIN_ASM.write_text("\n".join(lines) + "\n")


def build_lp_core(target, source, env):
    ULP_BUILD_DIR.mkdir(parents=True, exist_ok=True)
    ULP_GEN_DIR.mkdir(parents=True, exist_ok=True)

    print("Building LP-Core ULP binary for %s..." % mcu)

    print("  Preprocessing linker script...")
    preprocess_linker_script()

    app_sources = collect_ulp_sources()
    idf_sources = collect_idf_sources()
    all_sources = app_sources + idf_sources

    objects = []
    for src in all_sources:
        print("  Compiling %s..." % src.name)
        obj = compile_source(src)
        objects.append(obj)

    print("  Linking LP-Core ELF...")
    link_elf(objects)

    print("  Generating binary...")
    generate_binary()
    bin_size = LP_BIN.stat().st_size
    print("  LP-Core binary size: %d bytes" % bin_size)

    print("  Generating symbol header...")
    generate_symbol_header()

    print("  Generating binary header...")
    generate_binary_header()

    print("  Generating binary assembly...")
    generate_binary_assembly()

    print("LP-Core ULP binary built successfully")


#
# Patch memory.ld to reserve LP SRAM for the ULP binary
#
# The prebuilt memory.ld from framework-arduinoespressif32-libs may have
# lp_ram_seg starting at the base address without reserving space for the
# LP-Core binary. If custom_sdkconfig triggers a lib recompilation, the
# resulting memory.ld will already be correct — this patch only applies
# when needed.
#

_LP_RAM_SEG_RE = re.compile(
    r"(lp_ram_seg\s*\(RW\)\s*:\s*org\s*=\s*0x[0-9a-fA-F]+)"
    r"(\s*,\s*len\s*=\s*)"
    r"([^\n]+)"
)


def patch_memory_ld():
    fw_ld_dir = FW_LIBS_DIR / mcu / "ld"
    src_ld = fw_ld_dir / "memory.ld"

    if not src_ld.exists():
        return

    text = src_ld.read_text()

    # Skip if the linker script already accounts for ULP reserved memory
    # (e.g. from a custom_sdkconfig-triggered lib recompilation)
    if "+ %d" % ULP_RESERVE_MEM in text or "+%d" % ULP_RESERVE_MEM in text:
        print("memory.ld already reserves LP SRAM for ULP — no patching needed")
        return

    match = _LP_RAM_SEG_RE.search(text)
    if not match:
        sys.stderr.write(
            "Error: lp_ram_seg not found in %s — cannot reserve LP SRAM.\n"
            "  The LP-Core binary will fail to load at runtime.\n" % src_ld
        )
        env.Exit(1)
        print("memory.ld already reserves LP SRAM for ULP — no patching needed")
        return

    patched = _LP_RAM_SEG_RE.sub(
        r"\1 + %d\2\3 - %d" % (ULP_RESERVE_MEM, ULP_RESERVE_MEM),
        text,
    )

    patched_ld_dir = ULP_BUILD_DIR / "ld"
    patched_ld_dir.mkdir(parents=True, exist_ok=True)
    (patched_ld_dir / "memory.ld").write_text(patched)
    env.Prepend(LIBPATH=[str(patched_ld_dir)])
    print(
        "Patched memory.ld: lp_ram_seg offset by %d bytes for LP-Core binary"
        % ULP_RESERVE_MEM
    )


#
# SCons build graph
#

all_ulp_sources = collect_ulp_sources() + collect_idf_sources()
source_nodes = [str(s) for s in all_ulp_sources]
if SDKCONFIG_H.exists():
    source_nodes.append(str(SDKCONFIG_H))

ULP_BUILD_DIR.mkdir(parents=True, exist_ok=True)
ULP_GEN_DIR.mkdir(parents=True, exist_ok=True)

ulp_targets = [
    str(LP_BIN), str(LP_BIN_ASM),
    str(LP_HEADER), str(LP_BIN_HEADER), str(LP_MAPGEN_LD),
]
ulp_build = env.Command(
    ulp_targets,
    source_nodes,
    env.VerboseAction(build_lp_core, "Building LP-Core ULP binary"),
)

env.Depends(str(Path("$BUILD_DIR") / "${PROGNAME}.elf"), ulp_build)
env.Requires(str(Path("$BUILD_DIR") / "${PROGNAME}.elf"), ulp_build)

# The generated .S is compiled by the main toolchain and linked into the
# firmware, providing ulp_main_bin / ulp_main_bin_length symbols
ulp_bin_obj = env.Object(str(LP_BIN_ASM))
env.Requires(str(Path("$BUILD_DIR") / "${PROGNAME}.elf"), ulp_bin_obj)
env.AppendUnique(PIOBUILDFILES=ulp_bin_obj)

env.AppendUnique(CPPPATH=[
    str(ULP_GEN_DIR),
    str(IDF_COMPONENTS / "ulp" / "lp_core" / "include"),
    str(IDF_COMPONENTS / "ulp" / "ulp_common" / "include"),
])
env.Append(LINKFLAGS=["-T", str(LP_MAPGEN_LD)])

# Link libulp.a for ulp_lp_core_load_binary / ulp_lp_core_run
ulp_lib = FW_LIBS_DIR / mcu / "lib" / "libulp.a"
if ulp_lib.exists():
    env.Append(LIBS=[env.File(str(ulp_lib))])

patch_memory_ld()

print("LP-Core ULP support enabled for %s (reserve=%d bytes)" % (mcu, ULP_RESERVE_MEM))
