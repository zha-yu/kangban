/**
 * VRM 3D 舞台（Day 4 W1）：three 场景初始化、VRM 下载/解析、动画循环、完整 dispose。
 * 契约口径：
 * - C7 v1.1 缓存语义：version 只作不透明缓存键，值变了就重新拉模型（禁止大小比较）；
 *   version 未变且上次加载成功 → 跳过下载直接复用字节。
 * - C7 朝向：rotation_y_offset（弧度）由服务端下发（VRM0 朝向补偿，VRM1 恒 0.0），
 *   加载后原样应用到 vrm.scene 的 Y 旋转，前端不写死角度。
 * - PRD FR-3D-02：加载失败或超 5s 切静态头像——超时/失败由调用方兜底，本文件返回 null / 抛错。
 */
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils, type VRM } from "@pixiv/three-vrm";
import { LipsyncDriver, type GetAudioEl } from "./lipsync";

/** VRM 下载竞速超时（PRD FR-3D-02：>5s 切静态头像） */
const VRM_FETCH_TIMEOUT_MS = 5_000;
/** 头像景别：取包围盒高度的上部比例做头肩构图，占满圆框 */
const FRAME_HEIGHT_RATIO = 0.42;
/** 垂直视场角（度）：窄 fov 压缩透视畸变，适合正面头像 */
const CAMERA_FOV_DEG = 30;
/** pixelRatio 上限：高分屏下控制 GPU 负载 */
const MAX_PIXEL_RATIO = 2;
/** 包围盒异常（空模型）时的兜底景别：VRM 标准身高头部约在 y≈1.4m */
const FALLBACK_CAMERA_Y = 1.2;
const FALLBACK_CAMERA_Z = 1.8;

/* ---- VRM 字节缓存（C7 v1.1）---- */

interface VrmByteCache {
  version: number;
  buffer: ArrayBuffer;
}

/** 模块级单槽缓存：version 未变直接复用；变了则下载成功后整体覆盖 */
let vrmByteCache: VrmByteCache | null = null;

/**
 * 下载 .vrm 字节；version 命中缓存则跳过网络（C7 v1.1）。
 * 下载失败/超 5s 返回 null（旧缓存按 version 区分，不会被失败请求污染）。
 */
export async function fetchVrmBytes(
  url: string,
  version: number,
): Promise<ArrayBuffer | null> {
  if (vrmByteCache && vrmByteCache.version === version) return vrmByteCache.buffer;

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), VRM_FETCH_TIMEOUT_MS);
  let buffer: ArrayBuffer;
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) return null;
    buffer = await res.arrayBuffer();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
  // 仅下载成功才覆盖缓存；失败时旧键保留，version 未变的下次挂载仍可复用
  vrmByteCache = { version, buffer };
  return buffer;
}

/** 解析 VRM：gltf.userData.vrm 由 VRMLoaderPlugin 挂载（库未给 userData 类型，显式收窄） */
function parseVrm(buffer: ArrayBuffer): Promise<VRM> {
  return new Promise((resolve, reject) => {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    loader.parse(
      buffer,
      "",
      (gltf) => {
        const vrm = gltf.userData.vrm as VRM | undefined;
        if (vrm) resolve(vrm);
        else reject(new Error("gltf.userData.vrm missing"));
      },
      () => reject(new Error("gltf parse failed")),
    );
  });
}

/** 头像景别：以包围盒顶部（头部）为焦点做头肩构图，占满圆框 */
function frameCamera(camera: THREE.PerspectiveCamera, modelRoot: THREE.Object3D): void {
  const box = new THREE.Box3().setFromObject(modelRoot);
  const size = box.getSize(new THREE.Vector3());
  const frameHeight = size.y * FRAME_HEIGHT_RATIO;
  if (frameHeight <= 0) {
    // 兜底：包围盒异常（空模型）时退到固定景别，不让相机出现 NaN
    camera.position.set(0, FALLBACK_CAMERA_Y, FALLBACK_CAMERA_Z);
    camera.lookAt(0, FALLBACK_CAMERA_Y, 0);
    return;
  }
  // 焦点取头顶往下 frameHeight/2：头肩构图居中；视线沿 +Z（VRM1 面向 +Z，
  // VRM0 经服务端 rotation_y_offset 补偿后同样面向 +Z）
  const focusY = box.max.y - frameHeight / 2;
  const distance = frameHeight / (2 * Math.tan(THREE.MathUtils.degToRad(CAMERA_FOV_DEG / 2)));
  camera.position.set(0, focusY, distance);
  camera.lookAt(0, focusY, 0);
}

/** 挂载后的场景句柄：卸载时必须调用 dispose()，全量释放 GPU 资源 */
export interface VrmSceneHandle {
  dispose(): void;
}

/**
 * 挂载 VRM 场景：解析 → 朝向 → 取景 → 渲染循环。
 * 解析失败（非 VRM 资产等）会抛错，由调用方降级到静态头像。
 * getAudioEl：TTS 下行播放元素访问器（口型 RMS 能量源，W1 第 5 条）；
 * 不传或 aa 不可用时整个口型链路静默跳过。
 */
export async function mountVrmScene(
  canvas: HTMLCanvasElement,
  buffer: ArrayBuffer,
  rotationYOffset: number,
  getAudioEl?: GetAudioEl,
): Promise<VrmSceneHandle> {
  const vrm = await parseVrm(buffer);

  // 官方示例口径的瘦身：减顶点与合并骨架，降低 draw call 与编译开销
  VRMUtils.removeUnnecessaryVertices(vrm.scene);
  VRMUtils.combineSkeletons(vrm.scene);
  vrm.scene.traverse((obj) => {
    obj.frustumCulled = false; // 蒙皮网格包围盒不可靠，统一关闭裁剪
  });

  // C7：VRM0/VRM1 朝向差异完全由服务端下发的弧度补偿，不写死角度
  vrm.scene.rotation.y = rotationYOffset;
  console.info(`[avatar] VRM 加载成功 metaVersion=${vrm.meta.metaVersion}`);

  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setClearColor(0x000000, 0); // 透明底，圆框的 bg-brand-soft 透出
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO));

  const scene = new THREE.Scene();
  scene.add(vrm.scene);

  const light = new THREE.DirectionalLight(0xffffff, Math.PI);
  light.position.set(1, 1, 1);
  scene.add(light);

  const camera = new THREE.PerspectiveCamera(CAMERA_FOV_DEG, 1, 0.1, 20);
  frameCamera(camera, vrm.scene);

  // 画布尺寸随圆框收缩（min(560px, 48vh) 视口变化），CSS 管布局、这里只更新缓冲
  const resize = (): void => {
    const w = canvas.clientWidth || 1;
    const h = canvas.clientHeight || 1;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  resize();
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);

  // 渲染循环：vrm.update 驱动 springbone 等运行时组件
  // 口型（W1 第 5 条）：aa 可用且给了音频源访问器才建驱动；3.5.5 用 getExpression 判可用
  const expressionManager = vrm.expressionManager;
  const aaAvailable = (expressionManager?.getExpression("aa") ?? null) !== null;
  const lipsync = getAudioEl && aaAvailable ? new LipsyncDriver(getAudioEl) : null;
  const clock = new THREE.Clock();
  let raf = 0;
  const tick = (): void => {
    raf = requestAnimationFrame(tick);
    const level = lipsync?.tick(performance.now()) ?? 0;
    if (lipsync && expressionManager) expressionManager.setValue("aa", level);
    vrm.update(clock.getDelta());
    renderer.render(scene, camera);
  };
  tick();

  return {
    dispose(): void {
      cancelAnimationFrame(raf);
      lipsync?.dispose();
      observer.disconnect();
      scene.remove(vrm.scene);
      VRMUtils.deepDispose(vrm.scene); // 几何体 / 材质 / 纹理全量释放
      renderer.dispose();
    },
  };
}
