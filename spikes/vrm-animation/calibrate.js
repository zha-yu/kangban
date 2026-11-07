/**
 * 朝向标定页（Day05 A1 S3）：单 renderer 四象限同屏渲染 0 / +90° / 180° / -90°，
 * 看哪个象限露出正脸即该模型的 rotationYOffset。待机动画可选叠加（Q3 真机验证）。
 * 用法：calibrate.html?model=Yoimiya.vrm&anim=0
 */
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils, type VRM } from "@pixiv/three-vrm";
import { VRMAnimationLoaderPlugin, createVRMAnimationClip } from "@pixiv/three-vrm-animation";

const MODELS = [
  "Danheng.vrm", "Evelyn.vrm", "Firefly.vrm", "Fischl.vrm", "Jingyuan.vrm",
  "Jizi.vrm", "Linghua.vrm", "Lisa.vrm", "Neuvillette.vrm", "Ningguang.vrm",
  "Shenhe.vrm", "Sparkle.vrm", "Yinlin.vrm", "Yoimiya.vrm",
];
const OFFSETS = [0, Math.PI / 2, Math.PI, -Math.PI / 2];

const params = new URLSearchParams(location.search);
const statusEl = document.getElementById("status")!;
const canvas = document.getElementById("stage") as HTMLCanvasElement;
const animCheck = document.getElementById("anim") as HTMLInputElement;

const select = document.getElementById("model") as HTMLSelectElement;
for (const m of MODELS) {
  const opt = document.createElement("option");
  opt.value = m;
  opt.textContent = m;
  select.appendChild(opt);
}
const go = (m: string): void => {
  location.href = `calibrate.html?model=${encodeURIComponent(m)}${animCheck.checked ? "" : "&anim=0"}`;
};
document.getElementById("prev")!.onclick = () => go(MODELS[(MODELS.indexOf(select.value) + MODELS.length - 1) % MODELS.length]);
document.getElementById("next")!.onclick = () => go(MODELS[(MODELS.indexOf(select.value) + 1) % MODELS.length]);
select.onchange = () => go(select.value);

const model = params.get("model") ?? "Yoimiya.vrm";
if (!MODELS.includes(model)) {
  statusEl.textContent = `未知模型 ${model}`;
  throw new Error(model);
}
select.value = model;
if (params.get("anim") === "0") animCheck.checked = false;

function parseVrm(buffer: ArrayBuffer): Promise<VRM> {
  return new Promise((resolve, reject) => {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    loader.parse(buffer, "", (gltf) => {
      const vrm = gltf.userData.vrm as VRM | undefined;
      vrm ? resolve(vrm) : reject(new Error("no vrm"));
    }, () => reject(new Error("parse fail")));
  });
}

const vrm = await parseVrm(await (await fetch(`/${encodeURI(model)}`)).arrayBuffer());
VRMUtils.removeUnnecessaryVertices(vrm.scene);
VRMUtils.combineSkeletons(vrm.scene);

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(1);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x222831);
scene.add(vrm.scene);
scene.add(new THREE.AmbientLight(0xffffff, 1.2));
const key = new THREE.DirectionalLight(0xffffff, 2.2);
key.position.set(0.5, 1, 2);
scene.add(key);
const rim = new THREE.DirectionalLight(0x88aaff, 1.0);
rim.position.set(-1, 0.5, -1.5);
scene.add(rim);

const camera = new THREE.PerspectiveCamera(30, 4 / 3, 0.1, 20);
camera.position.set(0, 1.25, 2.6);
camera.lookAt(0, 1.1, 0);

/* 待机动画（Q3 真机验证：能播则标定时模型自然站立微动） */
let mixer: THREE.AnimationMixer | null = null;
if (animCheck.checked) {
  try {
    const animBuf = await (await fetch("/animations/common/idle.vrma")).arrayBuffer();
    const anim = await new Promise<NonNullable<unknown>>((resolve, reject) => {
      const loader = new GLTFLoader();
      loader.register((parser) => new VRMAnimationLoaderPlugin(parser));
      loader.parse(animBuf, "", (gltf) => {
        const a = gltf.userData.vrmAnimations?.[0];
        a ? resolve(a) : reject(new Error("no anim"));
      }, () => reject(new Error("anim parse fail")));
    });
    const clip = createVRMAnimationClip(anim as never, vrm);
    mixer = new THREE.AnimationMixer(vrm.scene);
    mixer.clipAction(clip).setLoop(THREE.LoopRepeat, Infinity);
    mixer.clipAction(clip).play();
  } catch (e) {
    statusEl.textContent = `idle.vrma 真机播放失败：${String(e)}（标定继续，无动画）`;
    mixer = null;
  }
}

const clock = new THREE.Clock();
const W = 800, H = 600, QW = W / 2, QH = H / 2;
renderer.setScissorTest(true);

const tick = (): void => {
  requestAnimationFrame(tick);
  mixer?.update(clock.getDelta());
  vrm.update(clock.getDelta());
  for (let i = 0; i < 4; i++) {
    const col = i % 2, row = Math.floor(i / 2);
    vrm.scene.rotation.y = OFFSETS[i];
    renderer.setViewport(col * QW, row === 0 ? QH : 0, QW, QH);
    renderer.setScissor(col * QW, row === 0 ? QH : 0, QW, QH);
    renderer.render(scene, camera);
  }
};
tick();

const animNote = mixer ? "｜idle.vrma 播放中 ✓" : "｜无动画";
statusEl.textContent = `当前：${model}${animNote}｜看哪个象限是正脸 → 该 offset 写入 avatar-catalog`;
document.title = "CALIBRATE_DONE";
