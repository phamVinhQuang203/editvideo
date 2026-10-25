# -*- coding: utf-8 -*-
"""NHÁNH 2 — GHÉP REVIEW: nhiều video CÙNG 1 sản phẩm -> phân tích, cắt các đoạn HOOK + đoạn
SALES/REVIEW (demo, tính năng, kết quả) -> RÁP thành nhiều video review MỚI, voice khớp từng cảnh.
Giữ ĐÚNG CẢNH ĐÚNG Ý: cảnh hook đứng đầu, các đoạn theo thứ tự review (vấn đề->demo->tính năng->kết quả).

Quy trình mỗi video nguồn:
  1) tách shot (scene-cut ffmpeg)  2) montage đánh số shot  3) whisper timestamps
  4) codex gán VAI từng shot (hook/problem/demo/feature/result/cta/filler) + điểm giữ
Gộp shot theo vai -> ráp M kịch bản cảnh khác nhau -> concat -> blur -> voice review theo cảnh.
"""
import os, re, time, json, random, shutil, subprocess, traceback
import engine as E
import translate as T
from hook_skill import TIKTOK_HOOK_3S_SKILL

RENDER_SEG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_segmented_vivibe.py")
FONT = "C\\:/Windows/Fonts/arial.ttf"     # cho drawtext đánh số shot
TARGET_W, TARGET_H = 1080, 1920
NORM_VF = (f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
           f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30")

ROLE_ORDER = {"hook":0, "problem":1, "comparison":2, "demo":3, "feature":4, "result":5, "cta":6, "filler":9}
BODY_ROLES = ("problem","demo","feature","result","comparison")

# ============ 7 KỊCH BẢN (theo framework user cung cấp) ============
# Mỗi kịch bản: flow cảnh + tone giọng + CẤU TRÚC lời + có dùng facts (thông số/giá) + CTA.
FRAMEWORKS = {
 "review_demo": {"label":"Review + Demo",
   "flow":["hook","problem","comparison","demo","feature","result"],
   "hook":"pain_point","facts":True,"cta":True,
   "tone":"thân mật như người đã dùng thật mách nhau, gần gũi nhiệt tình",
   "structure":"Mở HOOK vấn đề/thói quen phiền toái mà sản phẩm giải quyết → so sánh trải nghiệm cũ (cách làm cũ tốn công/khó chịu) → giới thiệu giải pháp → nói CÔNG NĂNG khi dùng → điểm các THÔNG SỐ chốt → giá/ưu đãi → CTA."},
 "test_real": {"label":"Test thực tế",
   "flow":["hook","problem","demo","result","comparison"],
   "hook":"cau_hoi","facts":True,"cta":True,
   "tone":"khách quan, người tiêu dùng tự kiểm chứng, không cường điệu, nói cả kỳ vọng lẫn kết quả thật",
   "structure":"HOOK đặt nghi ngờ về lời quảng cáo của sản phẩm ('nghe nói ..., thử xem đúng không') → thiết lập test → theo dõi kết quả → đối chứng với lời quảng cáo → kết luận + CTA."},
 "before_after": {"label":"Before – After",
   "flow":["comparison","problem","demo","result","feature"],
   "hook":"ket_qua_gay_to_mo","facts":True,"cta":True,
   "tone":"tối giản lời, để hình nói, chỉ 1-2 câu dẫn + 1 câu chốt cuối",
   "structure":"Reveal nhanh TRƯỚC/SAU ngay → hiện trạng trước → áp dụng sản phẩm → kết quả sau rõ khác biệt → chốt giá trị + CTA."},
 "storytelling": {"label":"Storytelling",
   "flow":["hook","problem","demo","result","feature"],
   "hook":"tinh_huong_that","facts":True,"cta":True,
   "tone":"kể chuyện gần gũi chân thực, cảm giác 'quả nhiên không mua nhầm'",
   "structure":"HOOK kể một tình huống thật liên quan sản phẩm → bối cảnh → mua/thử về dùng → kết quả tích cực → bằng chứng/thông số → CTA nhẹ nhàng thuyết phục."},
 "tips_hack": {"label":"Tips/Hack",
   "flow":["hook","demo","feature","result"],
   "hook":"ket_qua_gay_to_mo","facts":False,"cta":True,
   "tone":"chia sẻ mẹo, nhịp nhanh, cảm giác hữu ích hơn quảng cáo",
   "structure":"HOOK dạng mẹo hữu ích ('vài mẹo để ...') → 1-2 mẹo chung chung → mẹo cuối MỚI là sản phẩm này → CTA mềm."},
 "asmr": {"label":"ASMR",
   "flow":["hook","demo","result"],
   "hook":"ket_qua_gay_to_mo","facts":False,"cta":False,
   "tone":"gần như KHÔNG lời, chỉ 1 câu thì thầm rất nhẹ đầu và 1 câu cuối",
   "structure":"CỰC ÍT lời: 1 câu mở nhẹ → để hình + tiếng gốc dẫn → 1 câu chốt tên sản phẩm cuối. TUYỆT ĐỐI không đọc thông số/giá."},
 "lifestyle": {"label":"Lifestyle",
   "flow":["demo","feature","result"],
   "hook":"tinh_huong_that","facts":False,"cta":False,
   "tone":"nhật ký cá nhân đời thường, nhẹ nhàng, KHÔNG đọc thông số/giá",
   "structure":"Routine đời thường → vài hoạt động → sản phẩm xuất hiện thoáng qua như 1 chi tiết → lời nhẹ dạng nhật ký, CTA rất nhẹ hoặc không."},
}
# Ưu tiên storytelling cho video xào từ nhiều nguồn, sau đó tới các format
# kể chuyện/trải nghiệm; các format nặng claim được xếp sau.
FW_AUTO_SEQ = ["storytelling","before_after","lifestyle","review_demo","tips_hack","test_real","asmr"]

# ---------- 1. TÁCH SHOT ----------
def scene_cuts(src, thresh=0.35, min_len=1.2, max_shots=10):
    dur = E.probe(src) or 0
    if dur < 3: return [(0.0, dur)]
    r = E.ff(["ffmpeg","-hide_banner","-i",src,"-filter:v",
              f"select='gt(scene,{thresh})',showinfo","-f","null","-"])
    times = sorted(set(float(m) for m in re.findall(r"pts_time:([\d.]+)",
                    (r.stderr or b"").decode("utf-8","replace"))))
    bounds = [0.0] + [t for t in times if 0.3 < t < dur-0.3] + [dur]
    shots = []
    for i in range(len(bounds)-1):
        s, e = bounds[i], bounds[i+1]
        if e - s < min_len:
            if shots: shots[-1] = (shots[-1][0], e)      # gộp shot quá ngắn vào trước
            else: shots.append((s, e))
        else:
            shots.append((s, e))
    if len(shots) < 3:                                    # video tĩnh -> chia đều 6 đoạn
        n = 6; shots = [(dur*i/n, dur*(i+1)/n) for i in range(n)]
    if len(shots) > max_shots:                            # giữ shot DÀI nhất
        shots = sorted(sorted(shots, key=lambda x: x[1]-x[0], reverse=True)[:max_shots])
    return shots

# ---------- 2. MONTAGE ĐÁNH SỐ ----------
def shot_montage(src, shots, wd):
    for i, (s, e) in enumerate(shots):
        mid = (s + e) / 2.0
        vf = (f"scale=320:-1,drawtext=fontfile={FONT}:text='{i+1}':x=10:y=10:"
              f"fontsize=60:fontcolor=yellow:box=1:boxcolor=black@0.65:boxborderw=8")
        E.ff(["ffmpeg","-y","-ss",f"{mid:.2f}","-i",src,"-vf",vf,"-frames:v","1",
              os.path.join(wd, f"shot_{i:02d}.jpg")])
    cols = 4; rows = (len(shots) + cols - 1)//cols
    mon = os.path.join(wd, "montage.jpg")
    E.ff(["ffmpeg","-y","-framerate","1","-i",os.path.join(wd,"shot_%02d.jpg"),
          "-vf",f"tile={cols}x{rows}:padding=6:color=white","-frames:v","1",mon])
    return mon if os.path.exists(mon) else None

# ---------- 3+4. GÁN VAI TỪNG SHOT ----------
def classify_shots(mon, shots, seg_by_shot, product, niche):
    lines = []
    for i, (s, e) in enumerate(shots):
        lines.append(f"Shot {i+1} [{s:.1f}-{e:.1f}s]: TQ='{seg_by_shot[i][:60] or '(im lặng)'}'")
    prompt = f"""Bạn phân tích các SHOT của video review sản phẩm Douyin (sản phẩm: {product}; niche {niche}) để RÁP video review MỚI.
Ảnh montage đính kèm: mỗi ô có SỐ = shot đó (khớp danh sách dưới).
{chr(10).join(lines)}
Với MỖI shot, gán VAI trong luồng review:
- hook: cảnh có thể làm visual stopper ở 0-1 giây (chuyển động / kết quả / chi tiết lạ / tương phản),
  đồng thời đặt được câu hỏi tò mò và có payoff thật ở các shot sau
- problem: cảnh cho thấy VẤN ĐỀ trước khi dùng sản phẩm
- demo: cảnh THAO TÁC/DÙNG sản phẩm
- feature: cảnh khoe TÍNH NĂNG/chi tiết sản phẩm (cận cảnh, cấu tạo)
- result: cảnh KẾT QUẢ sau khi dùng (sạch/gọn/nhanh/xong)
- comparison: cảnh SO SÁNH trước-sau hoặc với cách cũ
- cta: cảnh kêu gọi/hiện giá/liên hệ
- filler: chuyển cảnh/logo/không giá trị
Với mỗi shot, thêm co_nguoi=true nếu thấy rõ mặt hoặc thân người. Nếu chỉ thấy bàn tay/cánh tay thao tác sản phẩm thì co_nguoi=false.
Trả DUY NHẤT 1 JSON: {{"shots":[{{"i":1,"vai":"hook","mo_ta":"<mô tả cảnh 1 câu tiếng Việt>","diem":0-10,"ro_san_pham":true|false,"co_nguoi":true|false}}, ...]}}
diem = độ ĐẮT của shot để đưa vào review (cảnh rõ, đẹp, thuyết phục = cao). Riêng vai hook,
ưu tiên khả năng chặn lướt trong 1 giây đầu và sự liên quan thật với phần thân video.
{TIKTOK_HOOK_3S_SKILL}
Đủ tất cả shot theo số."""
    j = E.codex_json(prompt, image=mon, effort="low", timeout=180) or {}
    out = j.get("shots") or []
    by_i = {int(x.get("i", 0)): x for x in out if isinstance(x, dict)}
    res = []
    for i, (s, e) in enumerate(shots):
        x = by_i.get(i+1, {})
        res.append({"src_i": None, "start": s, "end": e,
                    "vai": x.get("vai", "demo"), "mo_ta": x.get("mo_ta", ""),
                    "diem": int(x.get("diem", 5) or 5), "ro": bool(x.get("ro_san_pham", True)),
                    "co_nguoi": bool(x.get("co_nguoi", False))})
    return res

# ---------- CACHE phân tích (bỏ qua whisper+montage+codex nếu video đã phân tích) ----------
ACACHE = os.path.join(E.ROOT, "analysis_cache"); os.makedirs(ACACHE, exist_ok=True)

# ---------- prep + phân tích 1 nguồn ----------
def _ident(item):
    if item["type"] == "link":
        import hashlib; return "u" + hashlib.md5(item["url"].encode()).hexdigest()[:15]
    return E.file_hash(item["path"])

def analyze_source(item, si, wd, product_hint, niche, job, save, require_people_label=False):
    # CACHE HIT (theo URL/file gốc): dùng lại norm + vai cảnh, KHỎI tải + whisper + codex (~2ph/nguồn)
    ident = _ident(item)
    cdir = os.path.join(ACACHE, ident) if ident else None
    if cdir and os.path.exists(os.path.join(cdir, "meta.json")) and os.path.exists(os.path.join(cdir, "norm.mp4")):
        try:
            meta_c = json.load(open(os.path.join(cdir, "meta.json"), encoding="utf-8"))
            labeled = meta_c["labeled"]
            if require_people_label and not all("co_nguoi" in shot for shot in labeled):
                raise ValueError("cache cũ chưa có nhãn người")
            for L in labeled: L["src_i"] = si
            E.log(f"[{job['id']}] nguồn {si+1}: DÙNG LẠI phân tích cũ ({len(labeled)} cảnh, cache)", job); save(job)
            return os.path.join(cdir, "norm.mp4"), meta_c.get("zh",""), labeled
        except Exception:
            pass

    if item["type"] == "link":
        src, meta = E.download(item["url"], wd)
    else:
        src = os.path.join(wd, "src.mp4")
        E.ff(["ffmpeg","-y","-i",item["path"],*E.video_encode_args(),
              "-c:a","aac","-movflags","+faststart",src])
        meta = {"title": item.get("name","")}
    if not src or not os.path.exists(src) or E.probe(src) < 3:
        E.log(f"[{job['id']}] nguồn {si+1} lỗi/quá ngắn -> bỏ", job); save(job); return None, None, []

    norm = os.path.join(wd, "norm.mp4")     # chuẩn hoá 1080x1920 30fps để RÁP mượt
    E.ff(["ffmpeg","-y","-i",src,"-vf",NORM_VF,*E.video_encode_args(),
          "-c:a","aac","-ar","44100","-ac","2",norm])
    if not os.path.exists(norm): norm = src

    shots = scene_cuts(norm)
    E.log(f"[{job['id']}] nguồn {si+1}: {len(shots)} shot -> montage + nghe lời", job); save(job)
    mon = shot_montage(norm, shots, wd)
    zh, segs = E.transcribe(norm, wd, want_segments=True, language=None)   # TỰ NHẬN (Việt/không tiếng)
    seg_by_shot = []
    for (s, e) in shots:
        txt = " ".join(g["text"] for g in segs if g["end"] > s and g["start"] < e)
        seg_by_shot.append(txt.strip())
    E.log(f"[{job['id']}] nguồn {si+1}: não gán vai từng cảnh...", job); save(job)
    labeled = classify_shots(mon, shots, seg_by_shot, product_hint or "", niche)
    for i, L in enumerate(labeled):
        L["loi"] = seg_by_shot[i] if i < len(seg_by_shot) else ""   # LỜI GỐC cảnh -> để voice học cách nói thật
        L["src_i"] = si

    # LƯU CACHE (norm + vai cảnh, bỏ src_i vì theo job)
    if cdir:
        try:
            os.makedirs(cdir, exist_ok=True); shutil.copy2(norm, os.path.join(cdir, "norm.mp4"))
            save_lab = [{k: v for k, v in L.items() if k != "src_i"} for L in labeled]
            json.dump({"zh": zh or "", "labeled": save_lab}, open(os.path.join(cdir, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
    return norm, (zh or ""), labeled

# ---------- RÁP video từ danh sách clip ----------
MAX_CLIP = 2.5

def cut_clip(norm_paths, clip, wd, k):
    s, e = clip["start"], clip["end"]; dur = max(0.8, e - s)
    t = min(dur, MAX_CLIP)
    if dur > t + 0.3:
        s += (dur - t) * 0.35
    fr = max(1, int(t * 30)); inc = 0.13 / fr
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    pans = [f"x='{cx}':y='{cy}'",
            f"x='(iw-iw/zoom)*on/{fr}':y='{cy}'",
            f"x='(iw-iw/zoom)*(1-on/{fr})':y='{cy}'",
            f"x='{cx}':y='(ih-ih/zoom)*on/{fr}'"]
    vf = f"zoompan=z='min(zoom+{inc:.5f},1.13)':d=1:{pans[k % len(pans)]}:s=1080x1920:fps=30"
    out = os.path.join(wd, f"clip_{k:02d}.mp4")
    E.ff(["ffmpeg","-y","-ss",f"{s:.2f}","-i",norm_paths[clip["src_i"]],"-t",f"{t:.2f}",
          "-vf",vf,*E.video_encode_args(),"-r","30",
          "-c:a","aac","-ar","44100","-ac","2",out])
    if os.path.exists(out): return out
    # Dự phòng nếu zoom/pan không được codec hỗ trợ.
    E.ff(["ffmpeg","-y","-ss",f"{s:.2f}","-i",norm_paths[clip["src_i"]],"-t",f"{t:.2f}",
          "-vf",NORM_VF,*E.video_encode_args(),
          "-c:a","aac","-ar","44100","-ac","2",out])
    return out if os.path.exists(out) else None

def concat_clips(clip_files, out):
    lst = out + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for c in clip_files: f.write(f"file '{c.replace(chr(92),'/')}'\n")
    E.ff(["ffmpeg","-y","-f","concat","-safe","0","-i",lst,"-c","copy","-movflags","+faststart",out])
    if not os.path.exists(out):    # fallback re-encode concat
        E.ff(["ffmpeg","-y","-f","concat","-safe","0","-i",lst,*E.video_encode_args(),"-c:a","aac",out])
    return os.path.exists(out)

# ---------- ghi chú thứ tự cảnh (đưa vào write_script cho voice LIỀN MẠCH bám đúng diễn biến) ----------
def scene_note(ordered):
    VAI = {"hook":"mở/hook","problem":"vấn đề","demo":"thao tác dùng","feature":"khoe tính năng",
           "result":"kết quả","comparison":"so sánh","cta":"chốt"}
    flow = " → ".join(f"{VAI.get(c['vai'],c['vai'])}" for c in ordered)
    return ("Đây là VIDEO REVIEW đã GHÉP SẴN từ nhiều cảnh, chạy theo đúng thứ tự: " + flow +
            ". Viết lời LIỀN MẠCH như 1 người đang review, bám theo diễn biến trên hình từ đầu đến cuối.")

ALT_ROLE = {"problem":["comparison","hook","demo"],"comparison":["problem","result","feature"],
            "feature":["demo","result"],"result":["feature","demo","comparison"],
            "demo":["feature","result"],"hook":["problem","demo","result"],"cta":["result","feature"]}

# ---------- RÁP theo FLOW kịch bản: ÉP TRỘN NHIỀU NGUỒN + CHỐNG TRÙNG (toàn cục) ----------
def build_plan_fw(pool, fw, m, used_global, min_sources=2):
    """Chọn cảnh theo flow kịch bản. Ưu tiên: (1) chưa dùng ở bản khác, (2) NGUỒN khác các cảnh
    đã chọn trong bản này -> mỗi video mới trộn từ nhiều nguồn, các bản khác nhau."""
    by_role = {}
    for s in pool:
        by_role.setdefault(s["vai"], []).append(s)
    for r in by_role:
        by_role[r] = sorted(by_role[r], key=lambda x: -x["diem"])
    plan = []; plan_srcs = []; local = set()

    def key(c): return (c["src_i"], round(c["start"], 1))
    def cands_for(role):
        cs = by_role.get(role) or []
        if not cs:
            for a in ALT_ROLE.get(role, []):
                if by_role.get(a): return by_role[a]
        return cs
    def choose(role, avoid_src, avoid_global):
        cs = cands_for(role)
        for off in range(len(cs)):
            c = cs[(m + off) % len(cs)]                 # xoay theo m -> bản khác chọn cảnh khác
            if key(c) in local: continue
            if avoid_global and key(c) in used_global: continue
            if avoid_src and c["src_i"] in plan_srcs: continue
            return c
        return None

    for role in fw["flow"]:
        # ưu tiên chặt -> nới dần: khác nguồn+chưa dùng -> chưa dùng -> bất kỳ
        c = (choose(role, True, True) or choose(role, False, True)
             or choose(role, True, False) or choose(role, False, False))
        if c:
            plan.append(c); plan_srcs.append(c["src_i"]); local.add(key(c))

    # ÉP tối thiểu min_sources nguồn: nếu cả bản dồn 1 nguồn -> thay 1 cảnh (không phải hook) bằng nguồn khác
    if len(set(plan_srcs)) < min_sources and len(plan) >= 2:
        other = [s for s in pool if s["src_i"] not in set(plan_srcs) and key(s) not in used_global and key(s) not in local]
        other.sort(key=lambda x: -x["diem"])
        for idx in range(len(plan) - 1, 0, -1):         # thay từ cuối (giữ hook đầu)
            if other:
                plan[idx] = other.pop(0); local.add(key(plan[idx]))
                if len({c["src_i"] for c in plan}) >= min_sources: break

    if len(plan) < 2:                                    # bù nếu quá thiếu
        for s in sorted(pool, key=lambda x: -x["diem"]):
            if key(s) not in local: plan.append(s); local.add(key(s))
            if len(plan) >= 3: break

    # Thêm visual beat để video khoảng 25 giây có gần 10 nhịp cắt ngắn.
    target_clips = 10
    body = sorted([s for r in ("demo","feature","result","problem","comparison")
                   for s in (by_role.get(r) or [])], key=lambda x: -x["diem"])
    bi = m; guard = 0
    while len(plan) < target_clips and body and guard < len(body) * 2:
        c = body[bi % len(body)]; bi += 1; guard += 1
        if key(c) in local: continue
        plan.insert(max(1, len(plan) - 1), c)
        local.add(key(c))

    for c in plan: used_global.add(key(c))               # ghi CHỐNG TRÙNG cho các bản sau
    return plan

# kiểu hook mở đầu (giống autopilot) -> tạo NHIỀU phiên bản hook cho 1 video
HOOK_DESC = {
 "pain_point":       "MỞ bằng NỖI ĐAU/khó chịu thật mà sản phẩm này giải quyết (nêu đúng vấn đề của sản phẩm, lấy từ lời gốc).",
 "ket_qua_gay_to_mo":"MỞ bằng KẾT QUẢ bất ngờ khiến người xem tò mò 'làm sao được vậy', rồi mới lộ sản phẩm.",
 "cau_hoi":          "MỞ bằng CÂU HỎI trúng tình huống người xem hay gặp liên quan sản phẩm.",
 "tinh_huong_that":  "MỞ bằng TÌNH HUỐNG THẬT đời thường dẫn tới sản phẩm.",
}

# ---------- LỜI review LIỀN MẠCH theo KỊCH BẢN + FACTS (bám công năng, KHÔNG tả hình vụn) ----------
def review_script_fw(product, plan, fw, facts, dur, niche, contact=None, hook_type=None, address_style=0):
    tgt = max(12.0, min(38.0, dur - 1.0)); words = int(tgt * 4.1)
    hook_line = ("\nKIỂU HOOK MỞ ĐẦU (BẮT BUỘC): " + HOOK_DESC[hook_type]) if hook_type in HOOK_DESC else ""
    facts_block = ""
    if fw.get("facts"):
        bits = []
        if facts.get("specs"): bits.append(f"Thông số chốt: {facts['specs']}")
        if facts.get("price"): bits.append(f"Giá/ưu đãi: {facts['price']}")
        if bits:
            facts_block = "\nTHÔNG TIN SẢN PHẨM (đưa vào ĐÚNG đoạn theo cấu trúc, nói tự nhiên không đọc như bảng): " + " · ".join(bits)
    cta_txt = facts.get("cta") or "giỏ hàng góc trái"
    cta_line = f"\nKẾT: chốt CTA nhẹ nhàng kiểu \"{cta_txt}\"." if fw.get("cta") else "\nKhông hô hào mua, kết đọng tự nhiên."
    # mô tả cảnh dạng CHỮ (thay ảnh -> codex nhanh hơn nhiều + không fixation vào chi tiết hình)
    VAI = {"hook":"mở đầu","problem":"vấn đề","comparison":"so sánh","demo":"thao tác dùng",
           "feature":"tính năng","result":"kết quả","cta":"chốt"}
    flow_txt = " → ".join(f"{VAI.get(c['vai'],c['vai'])}" + (f" ({c['mo_ta']})" if c.get("mo_ta") else "")
                          for c in plan)
    polite_addresses = (
        'XƯNG HÔ BẮT BUỘC: người nói xưng "mình", gọi người xem là "bạn". '
        'Giữ tự nhiên và nhất quán từ đầu tới cuối.',
        'XƯNG HÔ BẮT BUỘC: người nói xưng "mình", gọi người xem là "các bạn". '
        'Giữ thân thiện và nhất quán từ đầu tới cuối.',
        'XƯNG HÔ BẮT BUỘC: người nói xưng "mình", gọi người xem là "mọi người". '
        'Giữ gần gũi và nhất quán từ đầu tới cuối.',
    )
    address_rule = polite_addresses[address_style % len(polite_addresses)]
    # LỜI GỐC từ chính video nguồn (các cảnh đã chọn) -> HỌC điểm bán + cách nói thật -> chống 1 màu
    orig = " ".join((c.get("loi") or "").strip() for c in plan if (c.get("loi") or "").strip())
    orig = " ".join(orig.split())[:900]
    orig_block = ("\n\nLỜI GỐC TỪ VIDEO NGUỒN (nguồn sự thật về sản phẩm này — HỌC điểm bán, con số, "
                  "cách gọi tên, chi tiết riêng; DIỄN ĐẠT LẠI tự nhiên theo tone+hook, KHÔNG chép nguyên "
                  "văn, KHÔNG bịa thông tin ngoài đây + facts):\n\"" + orig + "\"") if orig else ""
    prompt = f"""Viết LỜI THOẠI tiếng Việt LIỀN MẠCH (1 mạch, đọc 1 hơi) cho video review sản phẩm: {product} (niche {niche}).
KỊCH BẢN: {fw['label']}
TONE GIỌNG: {fw['tone']}
CẤU TRÚC BẮT BUỘC (bám sát): {fw['structure']}{hook_line}{facts_block}{cta_line}
Video chạy qua các cảnh theo thứ tự: {flow_txt}{orig_block}
{address_rule}

{TIKTOK_HOOK_3S_SKILL}
Hãy âm thầm tạo 5 hook, chấm đủ 5 tiêu chí và chọn hook tốt nhất trước khi viết. Chỉ xuất
lời thoại hoàn chỉnh của phương án thắng; không in danh sách ứng viên, bảng điểm hay giải thích.

NGUYÊN TẮC QUAN TRỌNG (đọc kỹ):
- CẤM dùng cách xưng hô "tao – mày", "chúng mày", "các con vợ", "các vợ yêu".
- Nội dung bám LỜI GỐC + facts sản phẩm ở trên (đúng sản phẩm này), KHÔNG viết chung chung.
- MỞ bằng HOOK đúng kiểu yêu cầu rồi dẫn tới sản phẩm; nói CÔNG NĂNG / LỢI ÍCH thật khi dùng.
- KHÔNG sa đà tả chi tiết hình ảnh vụn vặt; nói về GIÁ TRỊ sản phẩm.
- BÁM CẢNH: mỗi ý thoại phải khớp cảnh đang chiếu theo thứ tự; nói đúng điều cảnh đó minh hoạ.
- NHỊP NHANH, CÓ LỰC: hook vào thẳng, mạnh, gọn; nhấn keyword; câu ngắn, chuyển ý gọn; ít khoảng nghỉ; CTA dứt khoát.
- VIẾT CHO TTS: giữ một mạch nhưng phải có nhấn nhá; xen câu ngắn và câu vừa, dùng dấu phẩy
  để nghỉ nhẹ, dấu ba chấm cho điểm chờ, dấu hỏi/cảm thán ở hook hoặc điểm bất ngờ. Không
  viết hoa cả câu, không kéo dài mọi câu và không để giọng đều đều như đọc văn bản.
- CÁCH ĐỌC BẮT BUỘC: "3in1" viết thành "ba in một"; số giá dạng "139K" viết thành "139 nghìn"; thương hiệu "TIAN" viết thành "Ti An".
- PHONG CÁCH ADAM – DOMINANT, FIRM: tự nhiên, chắc giọng, dí dỏm như đang kể chuyện cho bạn bè; nhấn cảm xúc rõ ở hook, điểm bất ngờ và CTA.
- Tạo 2-4 điểm cảm xúc bằng lời nói thật như "Ê!", "Ủa?", "Trời ơi!", "Ha ha", "hì hì", "đó nhaaa"; có thể kéo nhẹ nguyên âm ở từ cảm thán nhưng không lạm dụng.
- {E.GENMAX_EMOTION_GUIDE}
- NGÔN TỪ/TONE: hài hước, vui vẻ, lầy nhẹ, nói như người thật đang tám chuyện; có câu chọc cười hoặc phản ứng bất ngờ nhưng không tục, không công kích người xem.
- VIẾT MỚI MẺ, KHÁC các bản khác: đổi cách mở, đổi câu chữ; TRÁNH khuôn sáo "Mấy chị mấy bà có thấy..."; ưu tiên chi tiết riêng từ lời gốc.
- Dài ~{tgt:.0f}s (~{words} từ, ±12). CHỈ in lời thoại thuần (không tiêu đề, không chú thích, không đánh số)."""
    return E.codex(prompt, effort="medium")   # medium -> lời đa dạng hơn (low bị 1 màu)

# ================= MAIN =================
def prepare_subtitle_source(assembled, workdir, mode, legacy_blur=False, progress=None, suffix="0"):
    mode = str(mode or ("smart_bottom" if legacy_blur else "keep")).lower()
    if mode not in ("keep", "bottom", "top", "smart_bottom", "smart_top"):
        mode = "smart_bottom"
    if mode == "keep":
        return assembled
    position = "top" if mode.endswith("top") else "bottom"
    blurred_path = os.path.join(workdir, f"assembled_subtitles_blurred_{suffix}.mp4")
    if mode in ("smart_bottom", "smart_top"):
        clean_path = os.path.join(workdir, f"assembled_without_subtitles_{suffix}.mp4")
        if progress: progress("Đang xóa sub gốc bằng OpenCV local...")
        if T.clean_original_subtitles(assembled, clean_path, position):
            if progress: progress("Đã xóa sub gốc")
            return clean_path
        if progress: progress("Xóa thông minh lỗi, chuyển sang làm mờ vùng sub...")
    elif progress:
        progress("Đang làm mờ vùng sub...")
    return blurred_path if T.blur_original_subtitles(assembled, blurred_path, position) else assembled


def prepare_audio(remove_original_audio=False):
    if remove_original_audio:
        return {"original": 0.0, "bed": None, "bedvol": 0.0}
    return {"original": 0.05, "bed": None, "bedvol": 0.07}


def run_review(job, save):
    o = job["opts"]; niche = o.get("niche","co-khi")
    M = int(o.get("n_variants", 3))
    product_hint = o.get("product","")
    outdir = os.path.join(E.OUT_DIR, job["id"]); os.makedirs(outdir, exist_ok=True)
    job["status"] = "running"; job["outputs"] = []; save(job)

    norm_paths = {}; pool = []; zh_all = []
    exclude_people = str(o.get("exclude_people", "")).lower() in ("1", "on", "true", "yes", "có")
    for si, item in enumerate(job["inputs"]):
        wd = os.path.join(E.WORK_DIR, job["id"], f"s{si}"); os.makedirs(wd, exist_ok=True)
        E.log(f"[{job['id']}] === phân tích nguồn {si+1}/{len(job['inputs'])} ===", job); save(job)
        norm, zh, labeled = analyze_source(item, si, wd, product_hint, niche, job, save,
                                           require_people_label=exclude_people)
        if not norm: continue
        norm_paths[si] = norm; zh_all.append(zh)
        pool.extend(labeled)
        job.setdefault("shots", []).extend([{"src": si+1, "vai": L["vai"], "mo_ta": L["mo_ta"],
                                             "diem": L["diem"]} for L in labeled])
        save(job)

    if exclude_people:
        before = len(pool)
        pool = [shot for shot in pool if not shot.get("co_nguoi", False)]
        E.log(f"[{job['id']}] loại {before-len(pool)} cảnh có người, giữ {len(pool)} cảnh", job); save(job)

    if len(norm_paths) == 0 or len(pool) < 3:
        job["status"] = "error"; job["error"] = "Không phân tích được nguồn nào (kiểm tra link/file)."
        save(job); return

    # xác định tên sản phẩm nếu chưa có
    product = product_hint
    if not product:
        best = sorted(pool, key=lambda x: -x["diem"])[:6]
        product = E.codex(f"Các cảnh review 1 sản phẩm: {[b['mo_ta'] for b in best]}. "
                          f"Trả về DUY NHẤT tên sản phẩm ngắn gọn tiếng Việt, không giải thích.").strip().splitlines()[-1][:50] or "sản phẩm"
    job["product"] = product; save(job)

    # FACTS sản phẩm (nhập tay) + chọn KỊCH BẢN cho từng biến thể
    facts = {"specs": o.get("specs","").strip(), "price": o.get("price","").strip(),
             "cta": o.get("cta","").strip()}
    fw_choice = o.get("framework","auto")
    if fw_choice in FRAMEWORKS:
        fw_keys = [fw_choice] * M                              # 1 kịch bản, khác nhau ở cảnh+giọng
    else:
        fw_keys = [FW_AUTO_SEQ[m % len(FW_AUTO_SEQ)] for m in range(M)]   # mỗi bản 1 KỊCH BẢN khác

    used_global = set()          # chống trùng cảnh giữa các bản
    n_src = len(norm_paths)
    min_src = min(3, n_src) if n_src >= 2 else 1        # ép trộn tối thiểu 2-3 nguồn/bản
    made = 0
    last_render_error = ""
    for m in range(M):
        fw = FRAMEWORKS[fw_keys[m]]
        plan = build_plan_fw(pool, fw, m, used_global, min_sources=min_src)
        if len(plan) < 2:
            E.log(f"[{job['id']}] bản {m+1}: không đủ cảnh cho kịch bản {fw['label']}", job); save(job); continue
        srcs = sorted({c["src_i"]+1 for c in plan})
        wd = os.path.join(E.WORK_DIR, job["id"], f"r{m}"); os.makedirs(wd, exist_ok=True)
        E.log(f"[{job['id']}] RÁP bản {m+1}/{M} [{fw['label']}]: {'->'.join(c['vai'] for c in plan)} | trộn nguồn {srcs}", job); save(job)

        # ===== 2a: NỐI hook + các đoạn khác theo FLOW kịch bản -> 1 video ghép =====
        clip_files = []
        for k, c in enumerate(plan):
            cf = cut_clip(norm_paths, c, wd, k)
            if cf: clip_files.append(cf)
        if len(clip_files) < 2:
            E.log(f"[{job['id']}] bản {m+1}: thiếu clip -> bỏ", job); save(job); continue
        assembled = os.path.join(wd, "assembled.mp4")
        if not concat_clips(clip_files, assembled):
            E.log(f"[{job['id']}] bản {m+1}: concat FAIL -> bỏ", job); save(job); continue
        dur = E.probe(assembled)

        # Dùng cùng cơ chế xóa sub của Dịch phim: OpenCV Telea và fallback FFmpeg blur.
        legacy_blur = str(o.get("blur", "")).lower() in ("1", "on", "true", "yes", "có")
        subtitle_mode = str(o.get("original_subtitles") or ("smart_bottom" if legacy_blur else "keep")).lower()
        def subtitle_progress(message):
            E.log(f"[{job['id']}] bản {m+1}: {message}", job); save(job)
        render_source = prepare_subtitle_source(assembled, wd, subtitle_mode, legacy_blur,
                                                subtitle_progress, str(m))
        au = prepare_audio(bool(o.get("remove_original_audio")))

        # ===== 2b: NHIỀU PHIÊN BẢN HOOK cho cùng video ghép (giống autopilot) =====
        # Mỗi phiên bản = 1 kiểu hook mở đầu khác + giọng khác -> lời khác hẳn, video ghép dùng chung.
        hv = max(1, min(4, int(o.get("hook_variants", 1) or 1)))
        hooks = [fw["hook"]] + [h for h in E.HOOKS if h != fw["hook"]]     # hook kịch bản trước, rồi các hook khác
        hooks = hooks[:hv]
        for hi, hk in enumerate(hooks):
            voice_id, vlabel = E.voice_from_choice(o.get("voice","auto"), niche, m*7 + hi)
            E.log(f"[{job['id']}] bản {m+1}.{hi+1} [{fw['label']}] hook={hk} viết lời + render ({vlabel})...", job); save(job)
            script = review_script_fw(product, plan, fw, facts, dur, niche, hook_type=hk,
                                      address_style=m + hi)
            sp = os.path.join(wd, f"script_{hi}.txt"); open(sp, "w", encoding="utf-8").write(script)
            outfn = f"review_{m+1}_{hk}.mp4" if hv > 1 else f"review_{m+1}.mp4"
            dstp = os.path.join(outdir, outfn)
            r = E.render(render_source, sp, dstp, voice_id, au, workdir=wd)
            if not r["ok"]:
                if r.get("err") == "vivibe_credit":
                    job["status"] = "error"; job["error"] = "Vivibe hết credit — nạp rồi chạy lại."; save(job); return
                last_render_error = r.get("err", "render fail")
                E.log(f"[{job['id']}] bản {m+1}.{hi+1} render FAIL: {last_render_error}", job); save(job); continue
            flip = o.get("flip","none")
            if flip and flip not in ("none","auto"): E.apply_xform(dstp, flip)
            thumb = os.path.join(outdir, outfn.replace(".mp4",".jpg"))
            E.ff(["ffmpeg","-y","-i",dstp,"-vf","scale=360:-2","-frames:v","1",thumb])
            fb_title = E.facebook_title(product, script, hk, niche, o.get("product_link", ""))
            job["outputs"].append({"file": outfn, "thumb": os.path.basename(thumb), "san_pham": product,
                "kich_ban": fw["label"], "canh": [c["vai"] for c in plan], "hook": hk,
                "nguon": f"{len(srcs)} nguồn", "voice": vlabel, "dur": round(E.probe(dstp),1),
                "fb_title": fb_title, "product_link": o.get("product_link", "")})
            made += 1
            E.log(f"[{job['id']}] ✓ {outfn} | {fw['label']} | hook {hk} | {vlabel}", job); save(job)
        shutil.rmtree(wd, ignore_errors=True)
    if made == 0:
        job["status"] = "error"
        job["error"] = (("Render/TTS thất bại: " + last_render_error[-300:]) if last_render_error
                        else "Không ráp được bản nào (thiếu cảnh).")
        save(job); return

    job["status"] = "done"; job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    E.log(f"[{job['id']}] XONG review: {len(job['outputs'])} video", job); save(job)


def run_safe(job, save):
    try:
        run_review(job, save)
    except Exception as e:
        job["status"] = "error"; job["error"] = f"{type(e).__name__}: {e}"
        job.setdefault("log", []).append(traceback.format_exc()[-1000:])
        save(job)
