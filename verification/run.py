import sys, importlib, os
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vt
mods = ["t_core", "t_cfg", "t_adm", "t_adv", "t_stress", "t_bonus", "t_more"]
args = sys.argv[1:]
out = "results.json"
if args and args[0].startswith("--out="): out = args[0][6:]; args = args[1:]
for m in mods:
    try: importlib.import_module(m)
    except ModuleNotFoundError as e:
        if e.name != m: raise
vt.run_tests(args or None, out)
