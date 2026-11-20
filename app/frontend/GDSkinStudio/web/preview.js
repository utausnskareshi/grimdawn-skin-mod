// GD Skin Studio の 3D プレビュー（計画書 9 章）。three.js は ./vendor/three に同梱（外部 CDN は使わない）。
// 画面（C#）とのやり取り: chrome.webview.postMessage / message イベント（docs/app_architecture.md 7 章）。
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const host = window.chrome && window.chrome.webview;
const send = (msg) => { if (host) host.postMessage(msg); };

const view = document.getElementById('view');
const overlay = document.getElementById('overlay');
const info = document.getElementById('info');
let strings = { loading: 'Loading…', empty: '', error: 'Error', height: 'height', vertices: 'vertices', triangles: 'triangles', frontLabel: 'Front (+Z)' };

// WebGL を使えない環境（3D の API を止めた PC など）では three.js が例外を投げ、以後の処理と 'ready' が無くなる。理由を
// 'fatal' で送ってから止め、画面（C#）が案内を出す（以前は「読み込み中…」のまま理由が出なかった。点検 R18B-05）
const renderer = (() => {
  try {
    return new THREE.WebGLRenderer({ antialias: true });
  } catch (e) {
    send({ event: 'fatal', kind: 'webgl', message: String(e && e.message ? e.message : e) });
    throw e;
  }
})();
renderer.setPixelRatio(window.devicePixelRatio || 1);
renderer.outputColorSpace = THREE.SRGBColorSpace;
view.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0xe9edf2);
scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8f99, 2.2));
const sun = new THREE.DirectionalLight(0xffffff, 1.6);
sun.position.set(2, 4, 3);
scene.add(sun);
const back = new THREE.DirectionalLight(0xffffff, 0.6);
back.position.set(-2, 2, -3);
scene.add(back);

const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 100);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.12;
controls.screenSpacePanning = true;

let grid = null;
let model = null;          // 読み込んだシーン（地面に置くための親）
let fit = { center: new THREE.Vector3(0, 1, 0), radius: 1 };
let textureOn = true;
let wireframe = false;
let currentDir = 'front';

// 必要なときだけ描く（three.js のマニュアル「Rendering on Demand」の方式。操作の無いときに画面のリフレッシュレートで描き続けない）。
// 操作・読み込み・向き・テクスチャ・ワイヤーフレーム・大きさの変更で requestRender を呼ぶ。減衰の間は render の中の
// controls.update() が change を出すので、カメラが止まるまで続けて描く
let renderRequested = false;
function render() {
  renderRequested = false;
  controls.update();
  renderer.render(scene, camera);
}
function requestRender() {
  if (renderRequested) return;
  renderRequested = true;
  requestAnimationFrame(render);
}
controls.addEventListener('change', requestRender);
// GPU のリセットの後や、ページがまた表示されたときにも描き直す（常に描き続けるループが無いため）
renderer.domElement.addEventListener('webglcontextrestored', requestRender);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') requestRender(); });

function resize() {
  const w = view.clientWidth || 1, h = view.clientHeight || 1;
  // 描画の解像度は 枠 × 表示倍率（125%・150% など）。canvas の表示の大きさは preview.css で枠に合わせる
  renderer.setPixelRatio(window.devicePixelRatio || 1);
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  render();                          // 大きさを変えると canvas が消えるので、次のフレームを待たずに描き直す
}
new ResizeObserver(resize).observe(view);
// 倍率の違うモニターへ移したときは枠の CSS の大きさが変わらず ResizeObserver が動かないので、倍率の変化も見張る
// （resolution の問い合わせは値が固定なので、変わるたびに今の倍率で作り直す）
function watchPixelRatio() {
  window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`)
    .addEventListener('change', () => { resize(); watchPixelRatio(); }, { once: true });
}
watchPixelRatio();
resize();

function setOverlay(text, isError) {
  overlay.textContent = text || '';
  overlay.className = text ? (isError ? 'error' : '') : 'hidden';
}

function disposeTexture(t) {
  if (!t || !t.isTexture) return;
  t.dispose();
  // GLTFLoader はテクスチャの画像を ImageBitmap で読む。dispose だけでは CPU 側の画像が GC まで残るので閉じる
  // （THREE.Cache は使っていないので、他のモデルと共有していない。2 回閉じても害は無い）
  const img = t.source && t.source.data;
  if (typeof ImageBitmap !== 'undefined' && img instanceof ImageBitmap) img.close();
}

function disposeObject(root) {
  root.traverse((o) => {
    if (!o.isMesh) return;
    o.geometry.dispose();
    for (const m of [].concat(o.material)) {
      disposeTexture(m.userData.origMap);
      for (const v of Object.values(m)) disposeTexture(v);   // map・roughnessMap など、材質の持つテクスチャ
      m.dispose();
    }
  });
}

function disposeModel() {
  if (!model) return;
  scene.remove(model);
  disposeObject(model);
  model = null;
  requestRender();
}

function applyMaterials() {
  if (!model) return;
  model.traverse((o) => {
    if (!o.isMesh) return;
    for (const m of [].concat(o.material)) {
      if (m.userData.origMap === undefined) m.userData.origMap = m.map || null;
      // 材質の色（glTF の baseColorFactor）も map と同じく残して戻す。変換は係数をテクスチャに焼き込むので、白で上書きすると
      // 係数で色を持つ GLB の「生成結果」だけが変換結果・ゲームと違う色に見えた（点検 R18A-03）
      if (m.userData.origColor === undefined) m.userData.origColor = m.color.clone();
      m.map = textureOn ? m.userData.origMap : null;
      if (!textureOn) m.color.set(0xc8ccd2); else m.color.copy(m.userData.origColor);
      m.wireframe = wireframe;
      m.needsUpdate = true;
    }
  });
  requestRender();
}

function setView(dir) {
  currentDir = dir || currentDir;
  // 包む球が縦・横どちらの画角にも収まる距離
  const vfov = THREE.MathUtils.degToRad(camera.fov);
  const hfov = 2 * Math.atan(Math.tan(vfov / 2) * camera.aspect);
  const d = fit.radius / Math.sin(Math.min(vfov, hfov) / 2) * 1.18;
  const c = fit.center;
  const pos = {
    front: [0, 0, d],        // 正面は +Z（TRELLIS・変換結果とも）
    back: [0, 0, -d],
    left: [d, 0, 0],         // キャラクターの左手は +X
    right: [-d, 0, 0],
    top: [0, d, 0.001],
    reset: [d * 0.55, d * 0.25, d * 0.8],
  }[currentDir] || [0, 0, d];
  camera.position.set(c.x + pos[0], c.y + pos[1], c.z + pos[2]);
  camera.near = Math.max(0.001, fit.radius / 100);
  camera.far = fit.radius * 50;
  camera.updateProjectionMatrix();
  controls.target.copy(c);
  controls.update();
  requestRender();
}

function rebuildGrid(size, step) {
  if (grid) { scene.remove(grid); grid.geometry.dispose(); grid.material.dispose(); }
  const divisions = Math.max(2, Math.round(size / step));
  grid = new THREE.GridHelper(divisions * step, divisions, 0x98a2b3, 0xc5ccd6);
  scene.add(grid);
  requestRender();
}

// 正面（+Z）の印（SF23、2026-10-17 U-F）: 床の上に原点から +Z 向きの矢印と「正面（+Z）」の文字を出す。ゲームのモデルの
// 正面は +Z（TRELLIS の出力の正面は −Z のこともある: docs/backlog.md B-004）。画面のチェックボックスで切り替え、既定は出さない。
// 出さない間は場面に何も加えないので、表示は今までと同じ。大きさは最後に読み込んだモデルに合わせる（モデルの扱い・材質・
// 読み込みの番号 loadSeq には触れない。テクスチャ・ワイヤーフレームの切り替えも印には効かない）
const FRONT_COLOR = 0x1d4ed8;
let frontOn = false;
let frontMarker = null;                    // THREE.Group（矢印と文字）。出していないときは null
let markerFit = { front: 0, radius: 1 };   // モデルの前の端の z と包む球の半径（モデルを読む前は最初の格子に合わせる）

function disposeFrontMarker() {
  if (!frontMarker) return;
  scene.remove(frontMarker);
  frontMarker.traverse((o) => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) { disposeTexture(o.material.map); o.material.dispose(); }
  });
  frontMarker = null;
}

function makeFrontLabel(text, height) {
  const px = 48, padX = 18, padY = 10;
  const font = `bold ${px}px "Yu Gothic UI", "Meiryo", "Segoe UI", sans-serif`;
  const c = document.createElement('canvas');
  const ctx = c.getContext('2d');
  ctx.font = font;
  c.width = Math.ceil(ctx.measureText(text).width) + padX * 2;
  c.height = px + padY * 2;
  ctx.font = font;                         // 大きさを変えると状態が戻るので設定し直す
  ctx.fillStyle = 'rgba(255,255,255,0.9)';
  ctx.strokeStyle = '#1d4ed8';
  ctx.lineWidth = 4;
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(2, 2, c.width - 4, c.height - 4, 12); else ctx.rect(2, 2, c.width - 4, c.height - 4);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = '#1d4ed8';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, c.width / 2, c.height / 2 + 2);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }));
  sprite.scale.set(height * c.width / c.height, height, 1);
  sprite.name = 'frontLabel';
  return sprite;
}

function buildFrontMarker() {
  const R = markerFit.radius;
  const headLen = 0.1 * R, headR = 0.04 * R, shaftR = 0.011 * R;
  const tip = markerFit.front + 0.2 * R;   // 矢印の先はモデルの前の端より少し前
  const shaftLen = Math.max(tip - headLen, 0.001 * R);
  const headZ = shaftLen + headLen / 2;
  const mat = () => new THREE.MeshBasicMaterial({ color: FRONT_COLOR });
  const g = new THREE.Group();
  g.name = 'frontMarker';
  const shaft = new THREE.Mesh(new THREE.CylinderGeometry(shaftR, shaftR, shaftLen, 12), mat());
  shaft.rotation.x = Math.PI / 2;          // 円柱・円錐の軸（+Y）を +Z に向ける
  shaft.position.set(0, headR, shaftLen / 2);
  const head = new THREE.Mesh(new THREE.ConeGeometry(headR, headLen, 24), mat());
  head.rotation.x = Math.PI / 2;
  head.position.set(0, headR, headZ);
  head.name = 'frontArrowHead';
  // 文字は矢印の先から立てた細い柱の上に置く。正面・斜めのカメラでは床の矢印が画面の下の端に来るので、文字を持ち上げて
  // 画面の端と左下の情報の表示に掛からないようにする（背面から見るとモデルの陰に隠れる）
  const poleTop = 0.27 * R;
  const pole = new THREE.Mesh(new THREE.CylinderGeometry(shaftR * 0.7, shaftR * 0.7, poleTop - headR, 8), mat());
  pole.position.set(0, headR + (poleTop - headR) / 2, headZ);
  const label = makeFrontLabel(strings.frontLabel || 'Front (+Z)', 0.14 * R);
  label.center.set(0.5, 0);                // 文字の下の端の中央を柱の上に合わせる
  label.position.set(0, poleTop, headZ);
  g.add(shaft, head, pole, label);
  return g;
}

function setFrontMarker(on) {
  frontOn = !!on;
  const had = !!frontMarker;
  disposeFrontMarker();
  if (frontOn) { frontMarker = buildFrontMarker(); scene.add(frontMarker); }
  if (had || frontOn) requestRender();
}

// 読み込みの番号。読み込みの途中で次の読み込みや clear が来たら（「180° 回して変換し直す」で 2 回続けて頼まれるときなど）、
// 古い読み込みの結果は場面に加えずに捨てる（'loaded'・'error' も送らない）
let loadSeq = 0;

async function load(url, label, opts) {
  opts = opts || {};
  const seq = ++loadSeq;
  disposeModel();
  info.textContent = '';
  setOverlay(strings.loading);
  const loader = new GLTFLoader();
  try {
    const gltf = await loader.loadAsync(url);
    const root = gltf.scene;
    if (seq !== loadSeq) { disposeObject(root); return; }
    let verts = 0, tris = 0;
    root.traverse((o) => {
      if (!o.isMesh) return;
      const g = o.geometry;
      if (!g.attributes.normal) g.computeVertexNormals();     // TRELLIS の GLB は法線を持たない
      verts += g.attributes.position.count;
      tris += g.index ? g.index.count / 3 : g.attributes.position.count / 3;
      for (const m of [].concat(o.material)) {
        m.side = THREE.DoubleSide;
        // ゲームは金属度を使わない（変換結果の GLB も 0）。TRELLIS の GLB は metallicFactor を持たず glTF の既定の 1 で
        // 暗い金属に見えるので、どの GLB も金属度 0 で見せる（glTF の metalnessMap は roughnessMap と同じテクスチャ）
        if (m.metalness !== undefined) { m.metalness = 0; m.metalnessMap = null; }
      }
    });
    // 地面（y=0）に置き、水平方向の中心を原点にする
    const box = new THREE.Box3().setFromObject(root);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const holder = new THREE.Group();
    root.position.set(-center.x, opts.keepGround ? 0 : -box.min.y, -center.z);
    holder.add(root);
    model = holder;
    scene.add(model);
    const h = opts.keepGround ? box.max.y : size.y;
    fit = { center: new THREE.Vector3(0, (opts.keepGround ? (box.min.y + box.max.y) / 2 : size.y / 2), 0),
            radius: Math.max(size.x, size.y, size.z) / 2 || 1 };
    const step = h > 1.5 ? 0.5 : 0.1;
    rebuildGrid(Math.max(size.x, size.z, h) * 2.2, step);
    markerFit = { front: size.z / 2, radius: fit.radius };   // 水平方向の中心は原点なので、前の端は z = 奥行きの半分
    if (frontOn) setFrontMarker(true);
    applyMaterials();
    setView(currentDir);
    setOverlay('');
    info.textContent = `${label || ''}  ${strings.vertices} ${verts.toLocaleString()} / ${strings.triangles} ${Math.round(tris).toLocaleString()}` +
                       `  ${strings.height} ${h.toFixed(3)}`;
    send({ event: 'loaded', label, vertices: verts, triangles: Math.round(tris), size: [size.x, size.y, size.z] });
  } catch (e) {
    if (seq !== loadSeq) return;     // 古い読み込みの失敗は、表示中のモデルの上に出さない
    setOverlay(strings.error + ': ' + (e && e.message ? e.message : e), true);
    send({ event: 'error', label, message: String(e && e.message ? e.message : e) });
  }
}

function clear(text) {
  loadSeq++;                         // 読み込みの途中なら、その結果は捨てる（'init' も clear を通る）
  disposeModel();
  info.textContent = '';
  setOverlay(text === undefined ? strings.empty : text);
}

if (host) {
  host.addEventListener('message', (ev) => {
    const m = ev.data || {};
    switch (m.cmd) {
      // 読み直しの後の 'ready' でも画面のチェックボックスの状態（frontMarker・テクスチャ・ワイヤーフレーム）が届く。文字も作り直す。
      // テクスチャ・ワイヤーフレームは続く 'load' の applyMaterials で効く（点検 R18A-04）
      case 'init':
        strings = Object.assign(strings, m.strings || {});
        if ('texture' in m) textureOn = !!m.texture;
        if ('wireframe' in m) wireframe = !!m.wireframe;
        clear(); if ('frontMarker' in m) setFrontMarker(m.frontMarker); break;
      case 'load': load(m.url, m.label, m); break;
      case 'clear': clear(m.text); break;
      case 'view': setView(m.dir); break;
      case 'texture': textureOn = !!m.on; applyMaterials(); break;
      case 'wireframe': wireframe = !!m.on; applyMaterials(); break;
      case 'frontMarker': setFrontMarker(m.on); break;
    }
  });
}

rebuildGrid(4, 0.5);
setView('reset');
// 開発時の確認用: WebView2 の外（普通のブラウザ）で ?glb=<URL>&view=<向き> を付けて開くと、その GLB を表示する（&front=1 で正面の印）
if (!host) {
  const q = new URLSearchParams(location.search);
  if (q.get('view')) currentDir = q.get('view');
  if (q.get('front') === '1') setFrontMarker(true);
  if (q.get('glb')) load(q.get('glb'), q.get('label') || '', { keepGround: q.get('ground') === 'keep' });
}
requestRender();
send({ event: 'ready' });
