/**
 * VRM 3D 舞台（Day 4 W1 建，Day 5 A1 S3 扩展）：three 场景初始化、VRM 下载/解析、
 * 程序化待机（眨眼/呼吸/视线）、.vrma 动画播放、动画循环、完整 dispose。
 * 契约口径（Day 5 A1 v1.4）：
 * - 模型来源为本地资产（frontend/public/assets/vrm/，目录见 avatar-catalog.ts），
 *   不再消费 C7 `GET /api/pad/avatar-config`（留痕 docs/team_tasks/day05/a1-本地VRM与形象选择页.md）。
 * - 朝向：rotationYOffset（弧度）由 avatar-catalog 静态配置（14 模型全 VRM0，S3 逐个标定回填）。
 * - 动画签名：createVRMAnimationClip(vrmAnimation, vrm)——动画在前（spike S1 实测约束）。
 * - PRD FR-3D-02：加载失败或超 5s 切静态头像——超时/失败由调用方兜底，本文件返回 null / 抛错。
 */
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils, type VRM } from "@pixiv/three-vrm";
import {
  VRMAnimationLoaderPlugin,
  createVRMAnimationClip,
  type VRMAnimation,
} from "@pixiv/three-vrm-animation";
import { LipsyncDriver, type GetAudioEl } from "./lipsync";

/** 资产下载竞速超时（PRD FR-3D-02：>5s 切静态头像；模型与动画共用） */
const FETCH_TIMEOUT_MS = 5_000;
/** 头像景别：取包围盒高度的上部比例做头肩构图，占满圆框 */
const FRAME_HEIGHT_RATIO = 0.42;
/** 垂直视场角（度）：窄 fov 压缩透视畸变，适合正面头像 */
const CAMERA_FOV_DEG = 30;
/** pixelRatio 上限：高分屏下控制 GPU 负载 */
const MAX_PIXEL_RATIO = 2;
/** 包围盒异常（空模型）时的兜底景别：VRM 标准身高头部约在 y≈1.4m */
const FALLBACK_CAMERA_Y = 1.2;
const FALLBACK_CAMERA_Z = 1.8;
/** 字节缓存槽位数（LRU）：主舞台当前模型 + 选择页预览来回切换不重复下载 */
const BYTE_CACHE_MAX = 3;
/** 眨眼节奏：下次眨眼间隔（ms，随机取区间值） */
const BLINK_INTERVAL_MIN_MS = 2_000;
const BLINK_INTERVAL_MAX_MS = 5_000;
/** 一次眨眼时长（ms）：闭半程 + 睁半程 */
const BLINK_DURATION_MS = 220;
/** 呼吸微摆幅度（弧度）与周期（秒）：胸口骨骼的微小起伏 */
const BREATH_AMPLITUDE_RAD = 0.02;
const BREATH_PERIOD_S = 4;

/* ---- 资产字节缓存（URL 键多槽 LRU）---- */

/**
 * 按 url 键的多槽 LRU 缓存：主舞台与选择页预览来回切换不重复下载。
 * 命中即提升到最新；超槽位淘汰最旧。仅下载成功才入缓存。
 */
const byteCache = new Map<string, ArrayBuffer>();

function cachePut(url: string, buffer: ArrayBuffer): void {
  byteCache.set(url, buffer);
  if (byteCache.size > BYTE_CACHE_MAX) {
    const oldest = byteCache.keys().next().value;
    if (oldest !== undefined) byteCache.delete(oldest);
  }
}

/**
 * 下载 .vrm / .vrma 字节；url 命中缓存则跳过网络。
 * 下载失败/超时返回 null（失败不污染缓存）。
 * 注：version 参数为 C7 时代的遗留签名，本地化后不再参与缓存键，S4 改造调用方后移除。
 */
export async function fetchVrmBytes(url: string, version?: number): Promise<ArrayBuffer | null> {
  void version;
  const hit = byteCache.get(url);
  if (hit !== undefined) {
    // LRU 提活：删了重插，保证 Map 迭代序即最近使用序
    byteCache.delete(url);
    byteCache.set(url, hit);
    return hit;
  }

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
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
  cachePut(url, buffer);
  return buffer;
}

/** 解析 VRM 模型：gltf.userData.vrm 由 VRMLoaderPlugin 挂载（库未给 userData 类型，显式收窄） */
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

/** 解析 .vrma 动画：gltf.userData.vrmAnimations 由 VRMAnimationLoaderPlugin 挂载 */
function parseVrmAnimation(buffer: ArrayBuffer): Promise<VRMAnimation> {
  return new Promise((resolve, reject) => {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMAnimationLoaderPlugin(parser));
    loader.parse(
      buffer,
      "",
      (gltf) => {
        const anim = gltf.userData.vrmAnimations?.[0] as VRMAnimation | undefined;
        if (anim) resolve(anim);
        else reject(new Error("gltf.userData.vrmAnimations missing"));
      },
      () => reject(new Error("vrma parse failed")),
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
  // VRM0 经 catalog rotationYOffset 补偿后同样面向 +Z）
  const focusY = box.max.y - frameHeight / 2;
  const distance = frameHeight / (2 * Math.tan(THREE.MathUtils.degToRad(CAMERA_FOV_DEG / 2)));
  camera.position.set(0, focusY, distance);
  camera.lookAt(0, focusY, 0);
}

/** 动画播放控制：一个 mixer，同一时刻至多一条动画（待机循环或一次性动作） */
interface AnimationState {
  mixer: THREE.AnimationMixer;
}

/** 挂载后的场景句柄：卸载时必须调用 dispose()，全量释放 GPU 资源 */
export interface VrmSceneHandle {
  dispose(): void;
  /**
   * 播放一段 .vrma：loop=true 循环（待机叠加），false 播一遍自动淡出回程序化待机。
   * 加载/解析失败静默返回 false（不动既有画面）；重复调用会切到新动画。
   */
  playAnimation(url: string, loop?: boolean): Promise<boolean>;
  /** 停止当前动画并回到程序化待机 */
  stopAnimation(): void;
}

/** S3 挂载选项：待机动画叠加（Q3） */
export interface VrmMountOptions {
  /** 待机 vrma 循环（如 /assets/vrm/animations/common/idle.vrma）；加载失败静默回程序化待机 */
  idleAnimationUrl?: string;
}

/**
 * 挂载 VRM 场景：解析 → 朝向 → 取景 → 程序化待机 + 动画播放 → 渲染循环。
 * 解析失败（非 VRM 资产等）会抛错，由调用方降级到静态头像。
 * getAudioEl：TTS 下行播放元素访问器（口型 RMS 能量源，W1 第 5 条）；
 * 不传或 aa 不可用时整个口型链路静默跳过（R8-b：无表情模型由调用方不传即可）。
 */
export async function mountVrmScene(
  canvas: HTMLCanvasElement,
  buffer: ArrayBuffer,
  rotationYOffset: number,
  getAudioEl?: GetAudioEl,
  opts: VrmMountOptions = {},
): Promise<VrmSceneHandle> {
  const vrm = await parseVrm(buffer);

  // 官方示例口径的瘦身：减顶点与合并骨架，降低 draw call 与编译开销
  VRMUtils.removeUnnecessaryVertices(vrm.scene);
  VRMUtils.combineSkeletons(vrm.scene);
  vrm.scene.traverse((obj) => {
    obj.frustumCulled = false; // 蒙皮网格包围盒不可靠，统一关闭裁剪
  });

  // 朝向：VRM0 统一走官方 rotateVRM0（本批 14 个模型全为 VRM0，S1 spike 实测）；
  // 个别模型如仍有偏差，用 avatar-catalog 的 rotationYOffset 逐个微调
  if (vrm.meta.metaVersion === "0") {
    VRMUtils.rotateVRM0(vrm);
  }
  vrm.scene.rotation.y += rotationYOffset;
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

  // 视线看向镜头（lookAt target 跟随相机位置）
  const lookTarget = new THREE.Object3D();
  lookTarget.position.copy(camera.position);
  scene.add(lookTarget);
  if (vrm.lookAt) vrm.lookAt.target = lookTarget;

  /* ---- 程序化待机：眨眼 + 呼吸微摆 ---- */

  const expressionManager = vrm.expressionManager;
  const blinkAvail = (expressionManager?.getExpression("blink") ?? null) !== null;
  const chest = vrm.humanoid?.getNormalizedBoneNode("chest") ?? null;
  const chestBaseRotationZ = chest?.rotation.z ?? 0;

  let nextBlinkAt = performance.now() + BLINK_INTERVAL_MIN_MS + Math.random() * (BLINK_INTERVAL_MAX_MS - BLINK_INTERVAL_MIN_MS);

  /* ---- vrma 动画播放（createVRMAnimationClip(anim, vrm)，S1 spike 约束）---- */

  let anim: AnimationState | null = null;

  async function playAnimation(url: string, loop = false): Promise<boolean> {
    const bytes = await fetchVrmBytes(url);
    if (!bytes) return false;
    let vrmAnim: VRMAnimation;
    try {
      vrmAnim = await parseVrmAnimation(bytes);
    } catch (e) {
      console.warn(`[avatar] 动画解析失败 ${url}`, e);
      return false;
    }
    stopAnimation();
    const clip = createVRMAnimationClip(vrmAnim, vrm);
    const mixer = new THREE.AnimationMixer(vrm.scene);
    const action = mixer.clipAction(clip);
    action.setLoop(loop ? THREE.LoopRepeat : THREE.LoopOnce, loop ? Infinity : 1);
    action.clampWhenFinished = !loop;
    if (!loop) {
      action.setEffectiveTimeScale(1);
      action.setEffectiveWeight(1);
      // 播完自动回程序化待机；stopAnimation 先 uncacheRoot 时该事件不会触发
      mixer.addEventListener("finished", () => stopAnimation());
      anim = { mixer };
    } else {
      anim = { mixer };
    }
    action.play();
    return true;
  }

  function stopAnimation(): void {
    if (!anim) return;
    anim.mixer.stopAllAction();
    anim.mixer.uncacheRoot(vrm.scene);
    anim = null;
  }

  // 画布尺寸随圆框收缩（min(560px, 48vh) 视口变化），CSS 管布局、这里只更新缓冲
  const resize = (): void => {
    const w = canvas.clientWidth || 1;
    const h = canvas.clientHeight || 1;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    lookTarget.position.copy(camera.position);
  };
  resize();
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);

  /* ---- 渲染循环 ---- */

  const aaAvailable = (expressionManager?.getExpression("aa") ?? null) !== null;
  const lipsync = getAudioEl && aaAvailable ? new LipsyncDriver(getAudioEl) : null;
  const clock = new THREE.Clock();
  let raf = 0;
  const tick = (): void => {
    raf = requestAnimationFrame(tick);
    const now = performance.now();
    const delta = clock.getDelta();

    // 动画优先：mixer 在写骨骼时程序化微摆让位（避免同帧双写打架）
    if (anim) anim.mixer.update(delta);

    // 口型（W1 第 5 条）：aa 可用且给了音频源才驱动；无表情模型（R8-b）调用方不传即跳过
    const level = lipsync?.tick(now) ?? 0;
    if (lipsync && expressionManager) expressionManager.setValue("aa", level);

    // 程序化待机：眨眼（叠在动画之上，与表情轨道不冲突）+ 呼吸微摆（仅无动画时）
    if (blinkAvail && expressionManager) {
      let blinkValue = 0;
      if (now >= nextBlinkAt) {
        const phase = (now - nextBlinkAt) / BLINK_DURATION_MS;
        if (phase >= 1) {
          nextBlinkAt =
            now + BLINK_INTERVAL_MIN_MS + Math.random() * (BLINK_INTERVAL_MAX_MS - BLINK_INTERVAL_MIN_MS);
        } else {
          blinkValue = Math.sin(Math.PI * phase);
        }
      }
      expressionManager.setValue("blink", blinkValue);
    }
    if (chest && !anim) {
      const t = clock.elapsedTime;
      chest.rotation.z = chestBaseRotationZ + Math.sin((t / BREATH_PERIOD_S) * Math.PI * 2) * BREATH_AMPLITUDE_RAD;
    }

    vrm.update(delta);
    renderer.render(scene, camera);
  };
  tick();

  // 待机动画叠加（Q3）：加载失败静默，不影响主画面
  if (opts.idleAnimationUrl) void playAnimation(opts.idleAnimationUrl, true);

  return {
    dispose(): void {
      cancelAnimationFrame(raf);
      stopAnimation();
      lipsync?.dispose();
      observer.disconnect();
      scene.remove(vrm.scene);
      VRMUtils.deepDispose(vrm.scene); // 几何体 / 材质 / 纹理全量释放
      renderer.dispose();
    },
    playAnimation,
    stopAnimation,
  };
}
