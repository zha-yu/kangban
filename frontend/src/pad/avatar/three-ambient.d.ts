/**
 * three 的最小环境类型声明（shim）。
 *
 * 为什么存在：three@0.187 的 npm 包不随包发布类型（build/ 下无 .d.ts），
 * @types/three 不在依赖清单内，且本任务红线禁止改 package.json / 引新依赖；
 * 因此按 pad 端 3D 展示（Day 4 W1）实际用到的 three API 做最小声明，只覆盖
 * vrm-scene.ts 用到的成员与 @pixiv/three-vrm 3.5.5 类型链路引用到的成员
 * （VRMCore.scene: Group、VRMUtils.deepDispose(object3D: Object3D) 等）。
 * 后续若解禁依赖，应删除本文件并改用 @types/three。
 *
 * 注意：本文件必须是「无顶层 import/export」的全局脚本文件，
 * 否则 declare module 会退化成模块扩展而失效。
 */

declare module "three" {
  export interface Euler {
    x: number;
    y: number;
    z: number;
  }

  export class Vector3 {
    x: number;
    y: number;
    z: number;
    constructor(x?: number, y?: number, z?: number);
    set(x: number, y: number, z: number): this;
  }

  export class Object3D {
    children: Object3D[];
    position: Vector3;
    rotation: Euler;
    frustumCulled: boolean;
    add(...object: Object3D[]): this;
    remove(...object: Object3D[]): this;
    clear(): this;
    traverse(callback: (object: Object3D) => void): void;
  }

  export class Group extends Object3D {}

  export class Scene extends Object3D {}

  export class Camera extends Object3D {
    lookAt(vector: Vector3): void;
    lookAt(x: number, y: number, z: number): void;
  }

  export class PerspectiveCamera extends Camera {
    constructor(fov?: number, aspect?: number, near?: number, far?: number);
    aspect: number;
    updateProjectionMatrix(): void;
  }

  export class DirectionalLight extends Object3D {
    constructor(color?: number, intensity?: number);
  }

  export interface Material {
    dispose(): void;
  }

  export interface Texture {
    dispose(): void;
  }

  export interface BufferGeometry {
    dispose(): void;
  }

  export class Box3 {
    min: Vector3;
    max: Vector3;
    constructor();
    setFromObject(object: Object3D): this;
    getSize(target: Vector3): Vector3;
  }

  export class Clock {
    constructor();
    getDelta(): number;
  }

  export const MathUtils: {
    degToRad(degrees: number): number;
  };

  export interface WebGLRendererParameters {
    canvas?: HTMLCanvasElement;
    alpha?: boolean;
    antialias?: boolean;
  }

  export class WebGLRenderer {
    constructor(parameters?: WebGLRendererParameters);
    setPixelRatio(value: number): void;
    setSize(width: number, height: number, updateStyle?: boolean): void;
    setClearColor(color: number, alpha?: number): void;
    render(scene: Object3D, camera: Camera): void;
    dispose(): void;
  }
}

/* GLTFLoader 用 three-vrm 自身 d.ts 引用的同一 specifier 声明，
   使 @pixiv/three-vrm 的 GLTFParser / GLTF 类型与本文件互通 */
declare module "three/examples/jsm/loaders/GLTFLoader.js" {
  import type { Group } from "three";

  export interface GLTFParser {
    [key: string]: unknown;
  }

  export interface GLTFLoaderPlugin {
    /* 标记接口：真实 three 同名接口即为空，避免索引签名挡住插件类赋值 */
  }

  export interface GLTF {
    scene: Group;
    scenes: Group[];
    /** vrm 字段由 VRMLoaderPlugin 在 afterRoot 时挂载，库未提供类型，调用处显式收窄 */
    userData: { vrm?: unknown } & Record<string, unknown>;
  }

  export class GLTFLoader {
    register(callback: (parser: GLTFParser) => GLTFLoaderPlugin): void;
    parse(
      data: ArrayBuffer | string,
      path: string,
      onLoad: (gltf: GLTF) => void,
      onError?: (event: Error) => void,
    ): void;
  }
}
