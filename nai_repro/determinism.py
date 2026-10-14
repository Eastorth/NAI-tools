import io, json, os
import numpy as np
from PIL import Image

D = r"D:\AI project\nai_repro\out\p0"
a_path = os.path.join(D, "p0_run1.png")
b_path = os.path.join(D, "p0_nai5-full.png")

a = np.asarray(Image.open(a_path).convert("RGB"), dtype=np.int16)
b = np.asarray(Image.open(b_path).convert("RGB"), dtype=np.int16)

print("run1 size:", a.shape, " run2 size:", b.shape)

diff = np.abs(a - b)
print("最大像素差 :", diff.max())
print("平均像素差 :", round(float(diff.mean()), 4))
print("完全相同像素占比:", round(float((diff.max(axis=2) == 0).mean()) * 100, 4), "%")
print("差异 >2 的像素占比:", round(float((diff.max(axis=2) > 2).mean()) * 100, 4), "%")
print("差异 >16 的像素占比:", round(float((diff.max(axis=2) > 16).mean()) * 100, 4), "%")

# 排除元数据后的原始 IDAT 对比
for p in (a_path, b_path):
    im = Image.open(p)
    c = im.info.get("Comment")
    d = json.loads(c) if isinstance(c, str) else c
    print(f"\n{os.path.basename(p)}")
    print("  signed_hash     :", d.get("signed_hash", "")[:40])
    print("  seed / steps / scale / sampler:", d.get("seed"), d.get("steps"), d.get("scale"), d.get("sampler"))
    print("  noise_schedule  :", d.get("noise_schedule"), "| cfg_rescale:", d.get("cfg_rescale"))
    print("  prefer_brownian :", d.get("prefer_brownian"),
          "| deliberate_euler_ancestral_bug:", d.get("deliberate_euler_ancestral_bug"))
    print("  model           :", d.get("model_name"), d.get("model_hash"))
