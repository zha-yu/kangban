/**
 * S1 spike：验证 @pixiv/three-vrm-animation@3.5.5 与 three@0.187 / three-vrm@3.5.5 协同，
 * 并产出「模型 × 动画」兼容矩阵（方案 §4.1 / day05 a1 任务卡 S1）。
 * 自动化：14 个本地模型逐个加载 → 对预载动画集逐个 createVRMAnimationClip + 播放若干帧，
 * 结果写入 DOM 表格并暴露 window.SPIKE_RESULT（JSON），供验收脚本读取。
 */
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";
import { VRMAnimationLoaderPlugin, createVRMAnimationClip } from "@pixiv/three-vrm-animation";

/** 14 个本地模型（frontend/public/assets/vrm/） */
const MODELS = [
  "Danheng.vrm", "Evelyn.vrm", "Firefly.vrm", "Fischl.vrm", "Jingyuan.vrm",
  "Jizi.vrm", "Linghua.vrm", "Lisa.vrm", "Neuvillette.vrm", "Ningguang.vrm",
  "Shenhe.vrm", "Sparkle.vrm", "Yinlin.vrm", "Yoimiya.vrm",
];

/** 动画子集：common 精选（含候选 idle）+ 原始库代表 + 日文文件名用例（R4） */
const ANIMS = [
  "animations/common/idle.vrma",
  "animations/common/idle-talking.vrma",
  "animations/common/nod.vrma",
  "animations/common/happy.vrma",
  "animations/common/raise-hand.vrma",
  "animations/VRMA_01.vrma",
  "animations/waiting-GVZZNZTH.vrma",
  "animations/liked-5GYAGDE6.vrma",
  "animations/dance.vrma",
  "animations/おじさん構文.vrma",
];

const statusEl = document.getElementById("status");
const outEl = document.getElementById("out");
const setProgress = (msg) => (statusEl.textContent = msg);

async function fetchBuffer(url) {
  const res = await fetch(encodeURI(url));
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.arrayBuffer();
}

/** 解析 VRM 模型 */
function parseVrm(buffer) {
  return new Promise((resolve, reject) => {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    loader.parse(buffer, "", (gltf) => {
      const vrm = gltf.userData.vrm;
      vrm ? resolve(vrm) : reject(new Error("userData.vrm missing"));
    }, () => reject(new Error("gltf parse failed")));
  });
}

/** 解析 VRM 动画（同一动画只解析一次，跨模型复用） */
function parseAnim(buffer) {
  return new Promise((resolve, reject) => {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMAnimationLoaderPlugin(parser));
    loader.parse(buffer, "", (gltf) => {
      const anim = gltf.userData.vrmAnimations?.[0];
      anim ? resolve(anim) : reject(new Error("userData.vrmAnimations missing"));
    }, () => reject(new Error("anim gltf parse failed")));
  });
}

const cell = (ok, note = "") => (ok ? `<span class="ok">✓</span>` : `<span class="fail" title="${note}">✗</span>`);

async function main() {
  // 1. 预载动画集（R4：日文文件名直接进 fetch，浏览器自动编码）
  setProgress("预载动画…");
  const animResults = [];
  const loadedAnims = [];
  for (const a of ANIMS) {
    try {
      const anim = await parseAnim(await fetchBuffer("/" + a));
      loadedAnims.push([a, anim]);
      animResults.push([a, true, ""]);
    } catch (e) {
      loadedAnims.push([a, null, String(e.message || e)]);
      animResults.push([a, false, String(e.message || e)]);
    }
  }

  // 2. 逐模型跑矩阵：加载 → 元信息 → 每个动画建 clip + 播 5 帧 → dispose
  const rows = [];
  const matrix = {}; // matrix[model] = { meta, humanoid, expr, anims: {anim: {ok, err}} }
  for (const m of MODELS) {
    setProgress(`测试模型 ${m} …`);
    const entry = { meta: "?", humanoid: false, expr: 0, anims: {} };
    let vrm = null;
    try {
      vrm = await parseVrm(await fetchBuffer("/" + m));
      entry.meta = vrm.meta?.metaVersion ?? "?";
      entry.humanoid = !!vrm.humanoid;
      // 3.5.5 的 expressionMap 返回 Record（对象），不是 Map，取键数
      const exprMap = vrm.expressionManager?.expressionMap;
      entry.expr = exprMap ? Object.keys(exprMap).length : 0;
    } catch (e) {
      entry.meta = "LOAD_FAIL";
      entry.animLoadErr = String(e.message || e);
    }
    if (vrm) {
      // 隐形画布上渲染 1 帧：抓渲染期报错（材质/着色器）。
      // 无 GPU/无 WebGL 的运行环境（headless）不算资产问题，记 SKIP
      let renderOk = true, renderErr = "";
      let renderSkip = false;
      let renderer = null;
      try {
        const canvas = document.createElement("canvas");
        canvas.width = 64; canvas.height = 64;
        renderer = new THREE.WebGLRenderer({ canvas, alpha: true });
        const scene = new THREE.Scene();
        scene.add(vrm.scene);
        const cam = new THREE.PerspectiveCamera(30, 1, 0.1, 20);
        cam.position.set(0, 1.2, 2);
        cam.lookAt(0, 1.2, 0);
        renderer.render(scene, cam);
      } catch (e) {
        const msg = String(e?.message || e);
        renderSkip = msg.includes("Error creating WebGL context");
        renderOk = !renderSkip;
        renderErr = renderSkip ? "SKIP: no WebGL in this environment" : msg;
      } finally {
        renderer?.dispose();
      }
      entry.renderOk = renderOk; entry.renderErr = renderErr; entry.renderSkip = renderSkip;

      for (const [a, anim, preErr] of loadedAnims) {
        if (!anim) { entry.anims[a] = { ok: false, err: `anim: ${preErr}` }; continue; }
        try {
          const clip = createVRMAnimationClip(anim, vrm); // 3.5.5 签名：(动画, 模型)
          if (clip.duration <= 0) throw new Error("clip.duration=0");
          const mixer = new THREE.AnimationMixer(vrm.scene);
          mixer.clipAction(clip).play();
          for (let i = 0; i < 5; i++) mixer.update(1 / 30);
          mixer.stopAllAction();
          entry.anims[a] = { ok: true, err: "" };
        } catch (e) {
          entry.anims[a] = { ok: false, err: String(e.message || e) };
        }
      }
      VRMUtils.deepDispose(vrm.scene);
    }
    matrix[m] = entry;
    rows.push(entry);
  }

  // 3. 渲染结果表
  const short = (a) => a.replace("animations/", "").replace("common/", "").replace(".vrma", "");
  let html = "<table><tr><th>模型</th><th>VRM</th><th>humanoid</th><th>表情</th><th>渲染</th>";
  for (const a of ANIMS) html += `<th title="${a}">${short(a)}</th>`;
  html += "</tr>";
  const allOk = [];
  for (const [m, e] of Object.entries(matrix)) {
    html += `<tr><td class="model">${m}</td><td class="${e.meta === "1" ? "ok" : "info"}">${e.meta}</td>`;
    html += `<td>${cell(e.humanoid)}</td><td>${e.expr}</td><td>${e.renderSkip ? `<span class="info" title="${e.renderErr}">−</span>` : cell(e.renderOk, e.renderErr)}</td>`;
    let rowOk = e.humanoid && (e.renderOk || e.renderSkip);
    for (const a of ANIMS) {
      const r = e.anims[a] ?? { ok: false, err: "skipped" };
      html += `<td>${cell(r.ok, r.err)}</td>`;
      if (!r.ok) rowOk = false;
    }
    html += "</tr>";
    if (rowOk) allOk.push(m);
  }
  html += "</table>";
  outEl.innerHTML = html;

  const summary = { anims: animResults.map(([a, ok, err]) => ({ file: a, ok, err })), matrix, allOkModels: allOk };
  window.SPIKE_RESULT = summary;
  const animOkCount = animResults.filter(([, ok]) => ok).length;
  setProgress(`完成：动画可解析 ${animOkCount}/${ANIMS.length}；全绿模型 ${allOk.length}/${MODELS.length}。结果已挂 window.SPIKE_RESULT`);
  document.title = "SPIKE_DONE";
}

main().catch((e) => {
  setProgress(`SPIKE 失败：${e?.stack || e}`);
  document.title = "SPIKE_ERROR";
});
