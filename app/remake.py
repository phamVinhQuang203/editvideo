# -*- coding: utf-8 -*-
"""NHÁNH 1 — REMAKE 1 video (link hoặc file) -> N biến thể video đầu ra.
Mỗi biến thể: lời thoại Việt khác (theo style/hook) + giọng + lật hình khác -> FB thấy khác.
Option: hook_type, voice, flip(lật), style, số biến thể, niche.
"""
import os, time, random, shutil, traceback
import engine as E

def _prep_source(item, wd):
    """item: {'type':'file'|'link','path'/'url','name'}. Trả (src_mp4, meta)."""
    if item["type"] == "link":
        src, meta = E.download(item["url"], wd)
        return src, meta
    p = item["path"]
    dst = os.path.join(wd, "src.mp4")
    # chuẩn hoá về mp4 h264 (input có thể mov/webm)
    E.ff(["ffmpeg","-y","-i",p,*E.video_encode_args(),
          "-c:a","aac","-movflags","+faststart",dst])
    if not os.path.exists(dst):
        shutil.copy2(p, dst)
    return dst, {"title": item.get("name",""), "url": ""}

def run_remake(job, save):
    """job['opts']: hook, voice, flip('auto'|list), style('auto'|key), n_variants, niche."""
    o = job["opts"]; niche = o.get("niche","co-khi")
    n = int(o.get("n_variants", 3))
    outdir = os.path.join(E.OUT_DIR, job["id"]); os.makedirs(outdir, exist_ok=True)
    job["status"] = "running"; job["outputs"] = []; save(job)

    pool = E.STYLE_POOL.get(niche) or list(E.KIEU_NOI)
    nn_flips = [x for x in E.VISUAL_XFORMS if x != "none"]

    for si, item in enumerate(job["inputs"]):
        wd = os.path.join(E.WORK_DIR, job["id"], f"s{si}"); os.makedirs(wd, exist_ok=True)
        E.log(f"[{job['id']}] nguồn {si+1}/{len(job['inputs'])}: chuẩn bị...", job); save(job)
        src, meta = _prep_source(item, wd)
        if not src or not os.path.exists(src):
            E.log(f"[{job['id']}] nguồn {si+1} TẢI/ĐỌC LỖI -> bỏ", job); save(job); continue
        dur = E.probe(src)
        if dur < 4:
            E.log(f"[{job['id']}] nguồn {si+1} quá ngắn ({dur:.1f}s) -> bỏ", job); save(job); continue

        contact = E.contact_sheet(src, os.path.join(wd, "contact.jpg"))
        E.log(f"[{job['id']}] nghe tiếng (whisper)...", job); save(job)
        zh, _ = E.transcribe(src, wd)
        E.log(f"[{job['id']}] não thẩm định...", job); save(job)
        br = E.brain(meta.get("title",""), zh, "", contact, dur, niche) or \
             {"san_pham": meta.get("title","sản phẩm"), "loai_audio":"mixed_voice_music",
              "form":"review_demo","hook_type":"pain_point","goc_content":"","diem_tiem_nang":7}
        job.setdefault("analysis", {})[str(si)] = {"san_pham": br.get("san_pham"),
            "diem": br.get("diem_tiem_nang"), "form": br.get("form"), "dur": round(dur,1)}
        save(job)

        au = E.autovoice_audio(br.get("loai_audio","mixed_voice_music"), src, wd)
        blur = E.blur_regions(src, wd)
        if blur: E.log(f"[{job['id']}] blur {len(blur)//2} vùng chữ TQ", job); save(job)

        # chọn style cho từng biến thể
        if o.get("style","auto") == "auto":
            styles = random.sample(pool, min(n, len(pool)))
            while len(styles) < n: styles.append(random.choice(pool))
        else:
            styles = [o["style"]] * n

        vi = 0
        for j in range(n):
            st = styles[j]
            hook = o.get("hook","auto")
            hook = ("" if hook == "auto" else hook)   # "" -> dùng hook của não
            voice_id, vlabel = E.voice_from_choice(o.get("voice","auto"), niche, vi); vi += 1
            E.log(f"[{job['id']}] biến thể {j+1}/{n}: viết lời ({st}) + render giọng {vlabel}...", job); save(job)
            script, ht = E.write_script(br, zh, contact, dur, hook_type=hook, style_key=st)
            sp = os.path.join(wd, f"script_{j}.txt"); open(sp,"w",encoding="utf-8").write(script)
            base = os.path.join(wd, f"base_{j}.mp4")
            r = E.render(src, sp, base, voice_id, au, blur_args=blur, workdir=wd)
            if not r["ok"]:
                if r.get("err") == "vivibe_credit":
                    job["status"] = "error"; job["error"] = "Vivibe hết credit — nạp rồi chạy lại."
                    E.log(f"[{job['id']}] DỪNG: Vivibe hết credit", job); save(job); return
                E.log(f"[{job['id']}] biến thể {j+1} render FAIL: {r.get('err','')}", job); save(job); continue
            if E.probe(base) < 6:
                E.log(f"[{job['id']}] biến thể {j+1} QC fail duration", job); save(job); continue

            # chọn lật hình
            flip_opt = o.get("flip","auto")
            if flip_opt == "none":
                xf = "none"
            elif flip_opt in ("auto", None):
                xf = "none" if j == 0 else nn_flips[(j-1) % len(nn_flips)]
            elif isinstance(flip_opt, list):
                xf = flip_opt[j % len(flip_opt)] if flip_opt else "none"
            else:
                xf = flip_opt   # 1 kiểu cố định
            outfn = f"video_{si+1}_{j+1}.mp4"
            dstp = os.path.join(outdir, outfn); shutil.copy2(base, dstp)
            if xf != "none": E.apply_xform(dstp, xf)
            # thumbnail
            thumb = os.path.join(outdir, outfn.replace(".mp4",".jpg"))
            E.ff(["ffmpeg","-y","-i",dstp,"-vf","scale=360:-2","-frames:v","1",thumb])
            fb_title = E.facebook_title(br.get("san_pham", ""), script, ht, niche, o.get("product_link", ""))
            job["outputs"].append({"file": outfn, "thumb": os.path.basename(thumb),
                "san_pham": br.get("san_pham",""), "style": st, "hook": ht,
                "voice": vlabel, "flip": E.XFORM_LABEL.get(xf, xf), "dur": round(E.probe(dstp),1),
                "fb_title": fb_title, "product_link": o.get("product_link", "")})
            E.log(f"[{job['id']}] ✓ {outfn} | {st} | hook {ht} | {vlabel} | {E.XFORM_LABEL.get(xf,xf)}", job)
            save(job)
            try: os.remove(base)
            except Exception: pass

        try: shutil.rmtree(wd, ignore_errors=True)
        except Exception: pass

    job["status"] = "done"; job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    E.log(f"[{job['id']}] XONG remake: {len(job['outputs'])} video", job); save(job)


def run_safe(job, save):
    try:
        run_remake(job, save)
    except Exception as e:
        job["status"] = "error"; job["error"] = f"{type(e).__name__}: {e}"
        job.setdefault("log", []).append(traceback.format_exc()[-800:])
        save(job)
