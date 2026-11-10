import importlib.util, json, os, sys
CODE = os.environ.get("ARIS_CODE_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, CODE)
spec = importlib.util.spec_from_file_location("aris", os.path.join(CODE, "aris.py"))
aris = importlib.util.module_from_spec(spec); sys.modules["aris"] = aris; spec.loader.exec_module(aris)
def call(name, **kw):
    return json.loads(aris._dispatch(name, kw))
