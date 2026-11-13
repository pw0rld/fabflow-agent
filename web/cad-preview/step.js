import * as THREE from './vendor/step/three.module.min.js';
import {OrbitControls} from './vendor/step/OrbitControls.js';
import './assembly-data.js';

const MAX_BYTES = 20 * 1024 * 1024;
const TIMEOUT_MS = 45000;

function abortError() {
  return new DOMException('预览已关闭。', 'AbortError');
}

function parse(bytes, signal, status) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError());
    const worker = new Worker(new URL('./step-worker.js', import.meta.url));
    let settled = false;
    const finish = (error, data) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      worker.terminate();
      if (error) reject(error); else resolve(data);
    };
    const onAbort = () => finish(abortError());
    const timer = setTimeout(() => finish(new Error('STEP 预览转换超时，请下载原件查看。')), TIMEOUT_MS);
    signal?.addEventListener('abort', onAbort, {once:true});
    worker.onerror = event => {
      event.preventDefault();
      finish(new Error('三维预览组件加载失败，请刷新后重试或下载原件。'));
    };
    worker.onmessage = ({data}) => {
      if (data?.stage === 'init') status('正在准备三维预览…');
      else if (data?.stage === 'parse') status('正在生成三维模型…');
      else if (data?.stage === 'error') finish(new Error(data.message));
      else if (data?.stage === 'ready') finish(null, data);
    };
    try {
      worker.postMessage({bytes}, [bytes]);
    } catch {
      finish(new Error('STEP 文件无法传入预览组件，请重新打开。'));
    }
  });
}

export async function mount({element, bytes, signal, status = () => {}, warning = () => {},
  onAssembly = () => {}, onSelectionChange = () => {}, upAxis = 'z', componentColors = false}) {
  if (!['y', 'z'].includes(upAxis)) throw new Error('预览向上方向无效。');
  if (signal?.aborted) throw abortError();
  if (!(bytes instanceof ArrayBuffer) || !bytes.byteLength || bytes.byteLength > MAX_BYTES) {
    throw new Error('STEP 文件为空或超过 20 MB 预览上限，请下载原件查看。');
  }
  status('正在读取 STEP 模型…');
  const data = await parse(bytes, signal, status);
  return mountData({element, data, signal, status, warning, onAssembly, onSelectionChange, upAxis, componentColors});
}

// These meshes come from native solid extraction. Their IDs and indices are
// independent of the old browser STEP preview snapshot.
export async function mountGeometry({geometry, groups, ...options}) {
  if (!geometry || !/^[a-f0-9]{64}$/.test(geometry.source_sha256) || geometry.output_unit !== 'mm' ||
      !Array.isArray(geometry.solids) || !geometry.solids.length || geometry.solids.length > 10000 ||
      !Array.isArray(groups) || !groups.length || groups.length > 10000) throw new Error('原生几何数据无效。');
  const solidIndices = new Map(), meshes = [];
  let vertices = 0, triangles = 0;
  for (const solid of geometry.solids) {
    if (typeof solid.id !== 'string' || solidIndices.has(solid.id)) throw new Error('原生实体编号无效。');
    const positions = solid.mesh?.positions, indices = solid.mesh?.indices;
    if (!Array.isArray(positions) || !positions.length || positions.length % 3 ||
        positions.some(value => typeof value !== 'number' || !Number.isFinite(value) || Math.abs(value) > 1e9) ||
        !Array.isArray(indices) || !indices.length || indices.length % 3 ||
        indices.some(value => !Number.isSafeInteger(value) || value < 0 || value >= positions.length / 3)) {
      throw new Error('原生实体的显示几何不完整。');
    }
    vertices += positions.length / 3; triangles += indices.length / 3;
    if (vertices > 2000000 || triangles > 1000000) throw new Error('原生模型超出浏览器显示范围。');
    solidIndices.set(solid.id, meshes.length);
    meshes.push({position:new Float32Array(positions), index:new Uint32Array(indices), normal:null,
      color:null, name:typeof solid.name === 'string' ? solid.name : solid.id});
  }
  const ids = new Set(), used = new Set(), rootId = 'native-root';
  const nodes = [{id:rootId, parent_id:null, name:'原生 CAD 实体', mesh_indices:[], children:[]}];
  for (const group of groups) {
    if (typeof group.id !== 'string' || !group.id || group.id === rootId || ids.has(group.id) ||
        !Array.isArray(group.solid_ids) || !group.solid_ids.length) throw new Error('制造分组编号无效。');
    ids.add(group.id);
    const references = group.solid_ids.map(id => {
      const index = solidIndices.get(id);
      if (index === undefined || used.has(id)) throw new Error('制造分组引用了重复或未知实体。');
      used.add(id); return index;
    });
    nodes[0].children.push(group.id);
    nodes.push({id:group.id, parent_id:rootId, name:typeof group.name === 'string' ? group.name : group.id,
      mesh_indices:references, children:[]});
  }
  if (used.size !== solidIndices.size) throw new Error('制造分组未覆盖全部原生实体。');
  const assembly = {schema_version:1, source_sha256:geometry.source_sha256, root_id:rootId, nodes,
    importer:{name:'native-occt', version:String(geometry.engine?.version || ''), output_unit:'mm'},
    meshes:geometry.solids.map((solid,index) => ({id:solid.id,index,name:solid.name || solid.id,
      vertex_count:meshes[index].position.length/3,triangle_count:meshes[index].index.length/3})),
    stats:{mesh_count:meshes.length,vertex_count:vertices,triangle_count:triangles}};
  return mountData({...options, componentColors:true, data:{assembly, meshes, vertices, triangles}});
}

// Original solid coordinates plus measured cylindrical-face meshes. This is
// a formed-part view, never a fabricated flat pattern.
export async function mountBends({solid, profile, sourceSha, ...options}) {
  if (!solid?.mesh || profile?.status !== 'candidate' || !Array.isArray(profile.bends) || !profile.bends.length) throw new Error('当前实体没有可显示的折弯几何。');
  const solids = [{id:'bend-base', name:'原始实体', mesh:solid.mesh}, ...profile.bends.map(bend => ({id:bend.id, name:bend.id, mesh:bend.mesh}))];
  const groups = solids.map(item => ({id:item.id, name:item.name, solid_ids:[item.id]}));
  return mountGeometry({...options, geometry:{source_sha256:sourceSha, output_unit:'mm', solids}, groups,
    bendDisplay:{baseId:'bend-base', bends:profile.bends}});
}

function mountData({element, data, signal, status = () => {}, warning = () => {},
  onAssembly = () => {}, onSelectionChange = () => {}, upAxis = 'z', componentColors = false, bendDisplay = null}) {
  if (!['y', 'z'].includes(upAxis)) throw new Error('预览向上方向无效。');
  if (signal?.aborted) throw abortError();
  if (!data.meshes?.length) throw new Error('此 STEP 没有可显示的三维实体。');
  const assembly = data.assembly;
  const hierarchy = globalThis.CadAssemblyData.index(assembly);
  // Assign colors to original component occurrences, not individual faces.
  const palette = [0x528dbd, 0x34a795, 0xe2b04b, 0x9476bf, 0xcf776c, 0x79a45c, 0x43a4b6, 0x6a79b6];
  const componentPalette = new Map(assembly.nodes.filter(node => node.mesh_indices.length)
    .map((node, index) => [node.id, palette[index % palette.length]]));
  let selectedId = null, isolatedId = null;
  const hiddenIds = new Set(), displayMeshes = [], bendLines = [];

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({antialias:true, alpha:false});
  } catch {
    throw new Error('当前浏览器无法启用三维显示，请更换浏览器或下载原件查看。');
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor('#eef2f6');
  const canvas = renderer.domElement;
  canvas.setAttribute('aria-label', 'STEP 三维预览，拖动旋转、滚轮缩放、右键平移');
  canvas.style.cssText = 'display:block;width:100%;height:100%;touch-action:none;';
  element.append(canvas);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 10000);
  camera.up.set(0, upAxis === 'y' ? 1 : 0, upAxis === 'z' ? 1 : 0);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  const group = new THREE.Group();
  scene.add(group);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8492a3, 2.5));
  const light = new THREE.DirectionalLight(0xffffff, 3);
  light.position.set(80, -50, 120);
  scene.add(light);
  let disposed = false, frame = 0, observer, needsRender = true;
  let pointerStart = null;
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();

  function drawable() {
    return !document.hidden && element.clientWidth > 0 && element.clientHeight > 0;
  }

  function requestRender() {
    if (disposed) return;
    needsRender = true;
    if (!frame && drawable()) frame = requestAnimationFrame(draw);
  }

  function draw() {
    frame = 0;
    if (disposed || !drawable()) return;
    // OrbitControls emits change while damping moves the camera. Only those
    // changes request another frame; an idle or hidden viewer does no work.
    const changed = controls.update();
    if (needsRender || changed) {
      needsRender = false;
      renderer.render(scene, camera);
    }
    if (changed) requestRender();
  }

  function visibilityChanged() {
    if (!document.hidden) { requestRender(); return; }
    cancelAnimationFrame(frame);
    frame = 0;
    needsRender = true;
  }
  controls.addEventListener('change', requestRender);
  document.addEventListener('visibilitychange', visibilityChanged);

  function assertNode(id) {
    if (id !== null && !hierarchy.nodes.has(id)) throw new Error('所选部件不属于当前模型。');
  }

  function highlight() {
    const selected = selectedId === null ? null : hierarchy.meshSets.get(selectedId);
    for (let index = 0; index < displayMeshes.length; index++) {
      const mesh = displayMeshes[index], material = mesh.material;
      const picked = !!selected?.has(index);
      if (bendDisplay) {
        const base = hierarchy.meshOwners.get(index) === bendDisplay.baseId;
        material.color.setHex(base ? 0xb7c7d3 : picked ? 0xff8b20 : 0xd88a3f);
        material.emissive.setHex(picked ? 0x703000 : 0x000000);
        material.emissiveIntensity = picked ? 0.22 : 0;
        material.opacity = base ? 0.18 : selectedId && !picked ? 0.48 : 1;
        material.transparent = base || !!(selectedId && !picked);
        material.depthWrite = !base;
        material.polygonOffset = !base; material.polygonOffsetFactor = -1; material.polygonOffsetUnits = -1;
        mesh.renderOrder = base ? 0 : 3;
        if (mesh.userData.edges) {
          const edges = mesh.userData.edges;
          edges.visible = true;
          edges.material.color.setHex(base ? 0x6d8b9f : picked ? 0xf2780e : 0xd48a31);
          edges.material.opacity = base ? 0.22 : !selectedId || picked ? 1 : 0.48;
          edges.material.depthTest = base; edges.material.depthWrite = false;
          edges.renderOrder = base ? 0 : 6;
        }
        continue;
      }
      if (!componentColors) {
        material.emissive.setHex(picked ? 0x15528f : 0x000000);
        material.emissiveIntensity = picked ? 0.7 : 0;
        continue;
      }
      const muted = selected !== null && !picked;
      material.color.copy(mesh.userData.baseColor);
      if (picked) material.color.setHex(0xf28a22);
      else if (muted) material.color.lerp(new THREE.Color(0xcbd5e1), 0.85);
      material.emissive.setHex(picked ? 0x542000 : 0x000000);
      material.emissiveIntensity = picked ? 0.18 : 0;
      material.opacity = muted ? 0.12 : 1;
      material.transparent = muted;
      material.depthWrite = !muted;
      if (mesh.userData.edges) {
        const edges = mesh.userData.edges;
        edges.visible = !muted;
        edges.material.color.setHex(picked ? 0x793500 : 0x31516b);
        edges.material.opacity = picked ? 1 : 0.65;
        edges.material.depthTest = !picked;
        edges.material.depthWrite = false;
        edges.renderOrder = picked ? 10 : 0;
      }
    }
    if (bendDisplay) {
      for (const line of bendLines) line.visible = !!selectedId && line.userData.bendId === selectedId;
      canvas.dataset.selectedBend = selectedId || '';
    }
    requestRender();
  }

  function select(id) {
    assertNode(id);
    if (disposed || selectedId === id) return;
    selectedId = id;
    highlight();
    onSelectionChange(id);
  }

  function pointerDown(event) {
    pointerStart = event.button === 0 ? {x:event.clientX, y:event.clientY, id:event.pointerId} : null;
  }

  function pointerUp(event) {
    const start = pointerStart;
    pointerStart = null;
    if (!start || event.button !== 0 || start.id !== event.pointerId ||
        Math.hypot(event.clientX - start.x, event.clientY - start.y) > 5 || disposed) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    pointer.set((event.clientX - rect.left) / rect.width * 2 - 1,
      -(event.clientY - rect.top) / rect.height * 2 + 1);
    scene.updateMatrixWorld(true);
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(displayMeshes.filter(mesh => mesh.visible && (!bendDisplay || hierarchy.meshOwners.get(mesh.userData.meshIndex) !== bendDisplay.baseId)), false)[0];
    if (bendDisplay && !hit) {
      // A genuine 1mm curved strip can be subpixel at full-part scale. Permit
      // selecting its actual boundary with a small screen-space tolerance,
      // without expanding or substituting the measured surface geometry.
      raycaster.params.Line.threshold = camera.position.distanceTo(controls.target) * 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) / rect.height * 4;
      const edges = displayMeshes.filter(mesh => mesh.visible && hierarchy.meshOwners.get(mesh.userData.meshIndex) !== bendDisplay.baseId).flatMap(mesh => mesh.userData.edges ? [mesh.userData.edges] : []);
      const boundary = raycaster.intersectObjects(edges, false)[0];
      if (boundary) { select(hierarchy.meshOwners.get(boundary.object.parent.userData.meshIndex) ?? null); return; }
    }
    select(hit ? hierarchy.meshOwners.get(hit.object.userData.meshIndex) ?? null : null);
  }
  const pointerCancel = () => {pointerStart = null;};

  function dispose() {
    if (disposed) return;
    disposed = true;
    cancelAnimationFrame(frame);
    observer?.disconnect();
    controls.removeEventListener('change', requestRender);
    document.removeEventListener('visibilitychange', visibilityChanged);
    signal?.removeEventListener('abort', dispose);
    canvas.removeEventListener('webglcontextlost', contextLost);
    canvas.removeEventListener('pointerdown', pointerDown);
    canvas.removeEventListener('pointerup', pointerUp);
    canvas.removeEventListener('pointercancel', pointerCancel);
    controls.dispose();
    group.traverse(object => {
      object.geometry?.dispose();
      if (Array.isArray(object.material)) object.material.forEach(item => item.dispose());
      else object.material?.dispose();
    });
    renderer.dispose();
    renderer.forceContextLoss();
    canvas.remove();
  }

  function contextLost(event) {
    event.preventDefault();
    if (!disposed) {
      warning('三维显示已中断，请重新打开预览或下载原件。');
      status('三维预览已中断。');
      dispose();
    }
  }
  canvas.addEventListener('webglcontextlost', contextLost);
  canvas.addEventListener('pointerdown', pointerDown);
  canvas.addEventListener('pointerup', pointerUp);
  canvas.addEventListener('pointercancel', pointerCancel);
  signal?.addEventListener('abort', dispose, {once:true});

  try {
    status('正在显示三维模型…');
    for (const item of data.meshes) {
      const meshIndex = displayMeshes.length;
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.BufferAttribute(item.position, 3));
      geometry.setIndex(new THREE.BufferAttribute(item.index, 1));
      if (item.normal) geometry.setAttribute('normal', new THREE.BufferAttribute(item.normal, 3));
      else geometry.computeVertexNormals();
      const material = new THREE.MeshStandardMaterial({
        color:componentColors ? new THREE.Color(componentPalette.get(hierarchy.meshOwners.get(meshIndex)) ?? palette[0]) :
          item.color ? new THREE.Color(...item.color) : new THREE.Color('#6c92ac'),
        roughness:componentColors ? 0.76 : 0.42, metalness:componentColors ? 0.05 : 0.28, side:THREE.DoubleSide
      });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.name = item.name;
      mesh.userData.meshIndex = displayMeshes.length;
      mesh.userData.baseColor = material.color.clone();
      displayMeshes.push(mesh);
      group.add(mesh);
      // Edges improve small manufactured part previews; skip the expensive pass for dense models.
      if (data.triangles <= 100000) {
        const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometry, 35),
          new THREE.LineBasicMaterial({color:'#31516b', transparent:true, opacity:componentColors ? 0.65 : 0.6,
            depthWrite:!componentColors}));
        mesh.userData.edges = edges;
        mesh.add(edges);
      }
    }
    if (bendDisplay) {
      for (const bend of bendDisplay.bends) for (const tangent of bend.tangent_lines || []) {
        if (![tangent.start, tangent.end].every(point => Array.isArray(point) && point.length === 3 && point.every(value => Number.isFinite(value) && Math.abs(value) <= 1e9))) throw new Error('折弯切线显示数据不完整。');
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute('position', new THREE.Float32BufferAttribute([...tangent.start, ...tangent.end], 3));
        const line = new THREE.Line(geometry, new THREE.LineBasicMaterial({color:0x147eab, transparent:true, opacity:0.95, depthTest:false, depthWrite:false}));
        line.userData.bendId = bend.id; line.renderOrder = 8; bendLines.push(line); group.add(line);
      }
      highlight();
      canvas.dataset.bendCount = String(bendDisplay.bends.length);
      canvas.setAttribute('aria-label', '折弯几何三维预览，橙色为真实曲带，蓝色为切线，点击曲带选择折弯');
    }
    const bounds = new THREE.Box3().setFromObject(group);
    const sphere = bounds.getBoundingSphere(new THREE.Sphere());
    if (!Number.isFinite(sphere.radius) || sphere.radius < 1e-9) {
      throw new Error('模型范围为空或无效，无法显示三维预览。');
    }
    const target = sphere.center.clone(), radius = sphere.radius;
    function fit(fitSphere) {
      if (disposed) return;
      if (!Number.isFinite(fitSphere.radius) || fitSphere.radius < 1e-9) return;
      controls.target.copy(fitSphere.center);
      const aspect = Math.max(camera.aspect, 0.1);
      const distance = fitSphere.radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * Math.max(1, 1 / aspect) * 1.12;
      const direction = upAxis === 'y' ? new THREE.Vector3(1, 0.8, -1) : new THREE.Vector3(1, -1, 0.8);
      camera.position.copy(fitSphere.center).add(direction.normalize().multiplyScalar(distance));
      camera.near = radius / 1000;
      camera.far = radius * 1000;
      controls.minDistance = radius * 0.05;
      controls.maxDistance = radius * 200;
      camera.updateProjectionMatrix();
      controls.update();
      requestRender();
    }
    function fitVisible() {
      const visibleBounds = new THREE.Box3();
      for (const mesh of displayMeshes) if (mesh.visible) visibleBounds.expandByObject(mesh);
      if (!visibleBounds.isEmpty()) fit(visibleBounds.getBoundingSphere(new THREE.Sphere()));
    }
    function reset() { fitVisible(); }
    function applyVisibility() {
      const isolated = isolatedId === null ? null : hierarchy.meshSets.get(isolatedId);
      const hidden = new Set();
      for (const id of hiddenIds) for (const mesh of hierarchy.meshSets.get(id)) hidden.add(mesh);
      displayMeshes.forEach((mesh, index) => {
        mesh.visible = (!isolated || isolated.has(index)) && !hidden.has(index);
      });
      requestRender();
    }
    function isolate(id) {
      assertNode(id);
      if (disposed) return;
      isolatedId = id;
      applyVisibility(); fitVisible();
    }
    function setHidden(id, hidden) {
      assertNode(id);
      if (id === null || typeof hidden !== 'boolean') throw new Error('部件显示状态无效。');
      if (disposed) return;
      if (hidden) hiddenIds.add(id); else hiddenIds.delete(id);
      applyVisibility();
    }
    function restoreVisibility() {
      if (disposed) return;
      isolatedId = null; hiddenIds.clear(); applyVisibility(); fit(sphere);
    }
    function getViewState() {
      return {selected_node_id:selectedId, isolated_node_id:isolatedId, hidden_node_ids:[...hiddenIds]};
    }
    function applyViewState(value) {
      const saved = globalThis.CadAssemblyData.viewState(value, hierarchy.nodes);
      if (disposed) return;
      selectedId = saved.selected_node_id; isolatedId = saved.isolated_node_id;
      hiddenIds.clear(); saved.hidden_node_ids.forEach(id => hiddenIds.add(id));
      applyVisibility(); highlight(); fitVisible();
    }
    function zoom(factor) {
      if (disposed || !Number.isFinite(factor) || factor <= 0) return;
      const offset = camera.position.clone().sub(controls.target);
      const distance = THREE.MathUtils.clamp(offset.length() / factor, controls.minDistance, controls.maxDistance);
      camera.position.copy(controls.target).add(offset.setLength(distance));
      controls.update();
      requestRender();
    }
    let previousAspect = null, hasSizedView = false;
    const resize = () => {
      if (disposed) return;
      const width = element.clientWidth, height = element.clientHeight;
      if (!width || !height) {
        cancelAnimationFrame(frame);
        frame = 0;
        needsRender = true;
        return;
      }
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      if (previousAspect !== null) {
        const fitRatio = Math.max(1, 1 / camera.aspect) / Math.max(1, 1 / previousAspect);
        const offset = camera.position.clone().sub(controls.target);
        camera.position.copy(controls.target).add(offset.multiplyScalar(fitRatio));
      }
      previousAspect = camera.aspect;
      camera.updateProjectionMatrix();
      // A viewer can first mount under a hidden tab. Its first real size must
      // fit the model too, including narrow restored/mobile viewports.
      if (!hasSizedView) { hasSizedView = true; fitVisible(); }
      requestRender();
    };
    observer = new ResizeObserver(resize);
    observer.observe(element);
    resize();
    reset();
    requestRender();
    status('三维模型已就绪，可拖动旋转。');
    warning('三维预览仅供查看，模型显示不代表材料、板厚或报价参数已核对。');
    onAssembly(assembly);
    return {dispose, reset, zoom, assembly, select, isolate, setHidden, restoreVisibility,
      getViewState, applyViewState};
  } catch (error) {
    dispose();
    throw error;
  }
}
