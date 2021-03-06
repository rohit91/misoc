#!/usr/bin/env python3

import sys
import os
import argparse
import subprocess
import struct
import shutil

from mibuild.tools import write_to_file
from migen.util.misc import autotype
from migen.fhdl import simplify

from misoclib.soc import cpuif
from misoclib.mem.sdram.phy import initsequence

from misoc_import import misoc_import


def _get_args():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
MiSoC - a high performance and small footprint SoC based on Migen.

This program builds and/or loads MiSoC components.
One or several actions can be specified:

clean           delete previous build(s).
build-bitstream build FPGA bitstream. Implies build-bios on targets with
                integrated BIOS.
build-headers   build software header files with CPU/CSR/IRQ/SDRAM_PHY definitions.
build-csr-csv   save CSR map into CSV file.
build-bios      build BIOS. Implies build-header.

load-bitstream  load bitstream into volatile storage.
flash-bitstream load bitstream into non-volatile storage.
flash-bios      load BIOS into non-volatile storage.

all             clean, build-bitstream, build-bios, flash-bitstream, flash-bios.

Load/flash actions use the existing outputs, and do not trigger new builds.
""")

    parser.add_argument("-t", "--target", default="mlabs_video", help="SoC type to build")
    parser.add_argument("-s", "--sub-target", default="", help="variant of the SoC type to build")
    parser.add_argument("-p", "--platform", default=None, help="platform to build for")
    parser.add_argument("-Ot", "--target-option", default=[], nargs=2, action="append", help="set target-specific option")
    parser.add_argument("-Op", "--platform-option", default=[], nargs=2, action="append", help="set platform-specific option")
    parser.add_argument("-X", "--external", default="", help="use external directory for targets, platforms and imports")
    parser.add_argument("--csr_csv", default="csr.csv", help="CSV file to save the CSR map into")

    parser.add_argument("-d", "--decorate", default=[], action="append", help="apply simplification decorator to top-level")
    parser.add_argument("-Ob", "--build-option", default=[], nargs=2, action="append", help="set build option")
    parser.add_argument("-f", "--flash-proxy-dir", default=None, help="set search directory for flash proxy bitstreams")

    parser.add_argument("action", nargs="+", help="specify an action")

    return parser.parse_args()

if __name__ == "__main__":
    args = _get_args()

    external_target = ""
    external_platform = ""
    if args.external:
        external_target = os.path.join(args.external, "targets")
        external_platform = os.path.join(args.external, "platforms")
        sys.path.insert(0, os.path.abspath(args.external))

    # create top-level SoC object
    target_module = misoc_import("targets", external_target, args.target)
    if args.sub_target:
        top_class = getattr(target_module, args.sub_target)
    else:
        top_class = target_module.default_subtarget

    if args.platform is None:
        if hasattr(top_class, "default_platform"):
            platform_name = top_class.default_platform
        else:
            raise ValueError("Target has no default platform, specify a platform with -p your_platform")
    else:
        platform_name = args.platform
    platform_module = misoc_import("mibuild.platforms", external_platform, platform_name)
    platform_kwargs = dict((k, autotype(v)) for k, v in args.platform_option)
    platform = platform_module.Platform(**platform_kwargs)
    if args.external:
        platform.soc_ext_path = os.path.abspath(args.external)

    build_name = args.target + "-" + top_class.__name__.lower() + "-" + platform_name
    top_kwargs = dict((k, autotype(v)) for k, v in args.target_option)
    soc = top_class(platform, **top_kwargs)
    soc.finalize()
    memory_regions = soc.get_memory_regions()
    csr_regions = soc.get_csr_regions()

    # decode actions
    action_list = ["clean", "build-bitstream", "build-headers", "build-csr-csv", "build-bios",
        "load-bitstream", "flash-bitstream", "flash-bios", "all"]
    actions = {k: False for k in action_list}
    for action in args.action:
        if action in actions:
            actions[action] = True
        else:
            print("Unknown action: {}. Valid actions are:".format(action))
            for a in action_list:
                print("  "+a)
            sys.exit(1)

    print("""\
                __  ___  _   ____     _____
               /  |/  / (_) / __/__  / ___/
              / /|_/ / / / _\ \/ _ \/ /__
             /_/  /_/ /_/ /___/\___/\___/

a high performance and small footprint SoC based on Migen

====== Building for: ======
Platform:  {}
Target:    {}
Subtarget: {}
CPU type:  {}
===========================""".format(platform_name, args.target, top_class.__name__, soc.cpu_type))

    # dependencies
    if actions["all"]:
        actions["clean"] = True
        actions["build-bitstream"] = True
        actions["build-bios"] = True
        if not actions["load-bitstream"]:
            actions["flash-bitstream"] = True
        if not soc.integrated_rom_size:
            actions["flash-bios"] = True
    if actions["build-bitstream"] and soc.integrated_rom_size:
        actions["build-bios"] = True
    if actions["build-bios"]:
        actions["build-headers"] = True

    if actions["clean"]:
        shutil.rmtree("build") # Need shell for the build/* globbing
        os.mkdir("build")
        subprocess.check_call(["make", "-C", os.path.join("software", "libcompiler-rt"), "clean"])
        subprocess.check_call(["make", "-C", os.path.join("software", "libbase"), "clean"])
        subprocess.check_call(["make", "-C", os.path.join("software", "libnet"), "clean"])
        subprocess.check_call(["make", "-C", os.path.join("software", "bios"), "clean"])

    if actions["build-headers"]:
        boilerplate = """/*
 * Platform:  {}
 * Target:    {}
 * Subtarget: {}
 * CPU type:  {}
 */

""".format(platform_name, args.target, top_class.__name__, soc.cpu_type)
        genhdir = os.path.join("software", "include", "generated")
        if soc.cpu_type != "none":
            cpu_mak = cpuif.get_cpu_mak(soc.cpu_type)
            write_to_file(os.path.join(genhdir, "cpu.mak"), cpu_mak)
            linker_output_format = cpuif.get_linker_output_format(soc.cpu_type)
            write_to_file(os.path.join(genhdir, "output_format.ld"), linker_output_format)

            linker_regions = cpuif.get_linker_regions(memory_regions)
            write_to_file(os.path.join(genhdir, "regions.ld"), boilerplate + linker_regions)

            for sdram_phy in ["sdrphy", "ddrphy"]:
                if hasattr(soc, sdram_phy):
                    sdram_phy_header = initsequence.get_sdram_phy_header(getattr(soc, sdram_phy).settings)
                    write_to_file(os.path.join(genhdir, "sdram_phy.h"), boilerplate + sdram_phy_header)
        mem_header = cpuif.get_mem_header(memory_regions, getattr(soc, "flash_boot_address", None))
        write_to_file(os.path.join(genhdir, "mem.h"), boilerplate + mem_header)
        csr_header = cpuif.get_csr_header(csr_regions, soc.get_constants())
        write_to_file(os.path.join(genhdir, "csr.h"), boilerplate + csr_header)

    if actions["build-csr-csv"]:
        csr_csv = cpuif.get_csr_csv(csr_regions)
        write_to_file(args.csr_csv, csr_csv)

    if actions["build-bios"]:
        ret = subprocess.call(["make", "-C", os.path.join("software", "bios")])
        if ret:
            raise OSError("BIOS build failed")

    bios_file = os.path.join("software", "bios", "bios.bin")

    if actions["build-bitstream"]:
        if soc.integrated_rom_size:
            with open(bios_file, "rb") as boot_file:
                boot_data = []
                while True:
                    w = boot_file.read(4)
                    if not w:
                        break
                    boot_data.append(struct.unpack(">I", w)[0])
            soc.init_rom(boot_data)

        for decorator in args.decorate:
            soc = getattr(simplify, decorator)(soc)
        build_kwargs = dict((k, autotype(v)) for k, v in args.build_option)
        vns = platform.build(soc, build_name=build_name, **build_kwargs)
        soc.do_exit(vns)

    if actions["load-bitstream"]:
        prog = platform.create_programmer()
        prog.load_bitstream(os.path.join("build", build_name + platform.bitstream_ext))

    if actions["flash-bitstream"]:
        prog = platform.create_programmer()
        prog.set_flash_proxy_dir(args.flash_proxy_dir)
        if prog.needs_bitreverse:
            flashbit = os.path.join("build", build_name + ".fpg")
            subprocess.check_call([os.path.join("tools", "byteswap"),
                os.path.join("build", build_name + ".bin"),
                flashbit])
        else:
            flashbit = os.path.join("build", build_name + ".bin")
        prog.flash(0, flashbit)

    if actions["flash-bios"]:
        prog = platform.create_programmer()
        prog.set_flash_proxy_dir(args.flash_proxy_dir)
        prog.flash(soc.cpu_reset_address, bios_file)
