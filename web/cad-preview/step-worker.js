/* STEP preview only. No quotation parameters or files are written. */
importScripts('./vendor/step/occt-import-js.js');
importScripts('./assembly-data.js');

const MAX_BYTES = 20 * 1024 * 1024;
const MAX_TRIANGLES = 1000000;
const MAX_VERTICES = 2000000;
const MAX_MESHES = 10000;
const MAX_COORDINATE = 1e9;
let started = false;

function failure(message) {
  throw new Error(message);
}

function finiteArray(values, maximum, label) {
  if (!Array.isArray(values) || values.length % 3 || values.length > maximum * 3) {
    failure(`模型${label}数量超出预览范围。`);
  }
  for (const value of values) {
    if (typeof value !== 'number' || !Number.isFinite(value) || Math.abs(value) > MAX_COORDINATE) {
      failure(`模型${label}无效，无法生成预览。`);
    }
  }
}

self.onmessage = async ({data}) => {
  if (started) return;
  started = true;
  try {
    if (!(data?.bytes instanceof ArrayBuffer) || !data.bytes.byteLength || data.bytes.byteLength > MAX_BYTES) {
      failure('STEP 文件为空或超过 20 MB 预览上限，请下载原件查看。');
    }
    const signature = new TextDecoder().decode(new Uint8Array(data.bytes, 0, Math.min(data.bytes.byteLength, 256)));
    if (!/^\s*ISO-10303-21\s*;/i.test(signature.replace(/^\uFEFF/, ''))) {
      failure('文件不是可识别的 STEP 文本，请下载原件核对格式。');
    }
    if (!self.crypto?.subtle) failure('浏览器无法校验模型原件，请使用 HTTPS 或本机地址打开。');
    const sourceDigest = await self.crypto.subtle.digest('SHA-256', data.bytes);
    const sourceSha256 = [...new Uint8Array(sourceDigest)].map(value => value.toString(16).padStart(2, '0')).join('');
    self.postMessage({stage:'init'});
    const occt = await occtimportjs({
      locateFile: name => new URL('./vendor/step/' + name, self.location.href).href,
      print: () => {},
      printErr: () => {}
    });
    self.postMessage({stage:'parse'});
    const result = occt.ReadStepFile(new Uint8Array(data.bytes), {
      linearUnit:'millimeter',
      linearDeflectionType:'bounding_box_ratio',
      linearDeflection:0.001,
      angularDeflection:0.5
    });
    if (!result?.success || !Array.isArray(result.meshes) || !result.meshes.length) {
      failure('此 STEP 未生成可显示的三维实体，请下载原件查看。');
    }
    if (result.meshes.length > MAX_MESHES) failure('模型实体过多，超过浏览器预览范围。');
    let triangles = 0, vertices = 0, faces = 0;
    const transfer = [];
    const meshes = result.meshes.map(item => {
      const positions = item?.attributes?.position?.array;
      finiteArray(positions, MAX_VERTICES, '顶点');
      if (!positions.length) failure('模型包含空网格，无法生成完整预览。');
      vertices += positions.length / 3;
      const indices = item?.index?.array;
      if (!Array.isArray(indices) || !indices.length || indices.length % 3) {
        failure('模型三角网格无效，无法预览。');
      }
      triangles += indices.length / 3;
      if (vertices > MAX_VERTICES || triangles > MAX_TRIANGLES) {
        failure('模型精度或复杂度超过浏览器预览范围，请下载原件查看。');
      }
      for (const index of indices) {
        if (!Number.isSafeInteger(index) || index < 0 || index >= positions.length / 3) {
          failure('模型三角网格索引无效，无法预览。');
        }
      }
      const position = new Float32Array(positions), index = new Uint32Array(indices);
      const normals = item.attributes.normal?.array;
      let normal = null;
      if (normals) {
        finiteArray(normals, MAX_VERTICES, '法线');
        if (normals.length !== positions.length) failure('模型法线数据不完整，无法预览。');
        normal = new Float32Array(normals);
        transfer.push(normal.buffer);
      }
      const color = Array.isArray(item.color) && item.color.length === 3 &&
        item.color.every(value => Number.isFinite(value) && value >= 0 && value <= 1) ? item.color : null;
      faces += Array.isArray(item.brep_faces) ? item.brep_faces.length : 0;
      transfer.push(position.buffer, index.buffer);
      return {position, normal, index, color, name:typeof item.name === 'string' ? item.name : ''};
    });
    const assembly = self.CadAssemblyData.build(result.root, result.meshes, sourceSha256);
    self.postMessage({stage:'ready', meshes, triangles, vertices, faces, assembly}, transfer);
  } catch (error) {
    const known = error instanceof Error && /[\u4e00-\u9fff]/.test(error.message);
    self.postMessage({stage:'error', message:known ? error.message : 'STEP 转换未完成，文件可能损坏或不受支持，请下载原件查看。'});
  }
};
