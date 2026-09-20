"""落轨逻辑回归测试（改 assemble_track / BORROW_SILENCE 后必跑）

验证四件事：
  ① 短句顺延后仍放得下（不丢字）
  ② 相邻句不重叠
  ③ 段间静音可被借用
  ④ 语音远超片长时顺延而非截断

用法: index-tts/.venv/bin/python pipeline/tests/test_layout.py
（需 numpy；用 index-tts 的 venv 因为它一定有）
"""
import os, sys, wave, shutil, importlib.util
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    "tts_mod", os.path.join(ROOT, "pipeline", "tts_dub_indextts.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
SR = 48000
class Args: pass
a = Args(); a.fill_window = 1.25; a.max_shift = 0.5; a.min_gap = 0.22

def case(name, defs):
    tdir=f"/tmp/dsh_laytest_{name}"; shutil.rmtree(tdir,ignore_errors=True); os.makedirs(tdir)
    segs,plan=[],[]
    for i,(st,en,du) in enumerate(defs,1):
        segs.append({"id":i,"start":st,"end":en,"en":f"l{i}","zh":f"中{i}"})
        plan.append({"id":i,"start":st,"end":en,"window":en-st+1.2,"rate":"clone",
                     "atempo":1.0,"dur":du,"overflow":0.0,"text":f"中{i}"})
        n=int(SR*du)
        with wave.open(f"{tdir}/seg_{i:04d}.wav","w") as w:
            w.setnchannels(2);w.setsampwidth(2);w.setframerate(SR)
            w.writeframes(np.repeat((np.sin(np.linspace(0,500,n))*0.3*32767).astype(np.int16)[:,None],2,axis=1).tobytes())
    return tdir,{"segments":segs},plan

def run(name,defs,expect_trunc):
    tdir,data,plan=case(name,defs)
    tr,ids=m.assemble_track(data,plan,tdir,a)
    ol=0; prev=None; lost=[]
    for p in plan:
        e=p["place_start"]+p["dur"]
        if prev is not None and p["place_start"]<prev-1e-6: ol+=1
        if e>p["hard_limit"]+0.001: lost.append(p["id"])
        prev=e
    ok = (ol==0) and (len(lost)==expect_trunc)
    print(f"{'✅' if ok else '❌'} {name}: 截断{tr} 重叠{ol} 丢字段{lost} (期望截断{expect_trunc})")
    return ok

r=[]
r.append(run("短句顺延放得下", [(0.0,1.0,1.0),(1.04,2.0,1.5),(2.04,3.0,1.6),(3.04,4.0,1.0)], 0))
r.append(run("大静音借用", [(0.0,1.0,1.0),(1.0,2.0,1.5),(5.0,7.0,2.0)], 0))
r.append(run("均匀节奏", [(0.0,2.0,1.5),(2.0,4.0,1.8),(4.0,6.0,1.6),(6.0,8.0,1.4)], 0))
r.append(run("单段", [(0.0,2.0,1.5)], 0))
r.append(run("语音远超片长(顺延不丢字)", [(0.0,1.0,3.0),(1.0,2.0,3.0)], 0))
print()
print("全部通过 ✅" if all(r) else "存在失败 ❌")
sys.exit(0 if all(r) else 1)
