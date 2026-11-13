/* Shared, dependency-free STEP display metadata. No manufacturing inference. */
(function (scope) {
  'use strict';
  const MAX_NODES = 20000, MAX_DEPTH = 64, MAX_NAME = 512;
  function fail(message) { throw new Error(message); }
  function name(value) {
    if (value === undefined || value === null) return '';
    if (typeof value !== 'string' || value.length > MAX_NAME) fail('模型部件名称无效或过长。');
    return value;
  }
  function build(root, meshes, sourceSha256) {
    if (!/^[a-f0-9]{64}$/.test(sourceSha256)) fail('模型原件指纹无效。');
    const nodes = [], used = new Set(), warnings = [];
    let hierarchyOrigin = 'source';
    const seen = new Set();
    function visit(item, id, parent, depth) {
      if (!item || typeof item !== 'object' || Array.isArray(item) || seen.has(item) ||
          depth > MAX_DEPTH || nodes.length >= MAX_NODES) fail('模型部件层级无效或超出显示范围。');
      seen.add(item);
      const references = item.meshes === undefined ? [] : item.meshes;
      const children = item.children === undefined ? [] : item.children;
      if (!Array.isArray(references) || !Array.isArray(children) || children.length > MAX_NODES) {
        fail('模型部件关联无效。');
      }
      const unique = new Set();
      for (const index of references) {
        if (!Number.isSafeInteger(index) || index < 0 || index >= meshes.length || unique.has(index)) {
          fail('模型部件引用了无效的几何。');
        }
        unique.add(index); used.add(index);
      }
      const node = {id, parent_id:parent, name:name(item.name), mesh_indices:[...unique], children:[]};
      nodes.push(node);
      children.forEach((child, index) => {
        const childId = id + '-' + index;
        node.children.push(childId);
        visit(child, childId, id, depth + 1);
      });
    }
    if (!root) {
      hierarchyOrigin = 'generated';
      warnings.push('原件没有可用的装配层级，以下按显示几何列出，尚未确定制造零件。');
      root = {name:'未分组几何', children:meshes.map((mesh, index) => ({name:mesh.name || '', meshes:[index]}))};
    }
    visit(root, 'n-0', null, 0);
    const missing = meshes.map((_, index) => index).filter(index => !used.has(index));
    if (missing.length) {
      warnings.push('部分显示几何未关联原装配节点，已放入未归组项，需人工核对。');
      const id = 'n-0-' + nodes[0].children.length;
      nodes[0].children.push(id);
      visit({name:'未归组几何', meshes:missing}, id, 'n-0', 1);
    }
    const displayMeshes = meshes.map((mesh, index) => ({id:'m-' + index, index,
      name:name(mesh.name), vertex_count:mesh.attributes.position.array.length / 3,
      triangle_count:mesh.index.array.length / 3}));
    return {schema_version:1, source_sha256:sourceSha256,
      importer:{name:'occt-import-js', version:'0.0.23', output_unit:'mm'},
      root_id:'n-0', hierarchy_origin:hierarchyOrigin, warnings, nodes, meshes:displayMeshes,
      stats:{mesh_count:meshes.length,
        triangle_count:displayMeshes.reduce((total, mesh) => total + mesh.triangle_count, 0),
        vertex_count:displayMeshes.reduce((total, mesh) => total + mesh.vertex_count, 0)}};
  }
  function index(assembly) {
    const nodes = new Map(assembly.nodes.map(node => [node.id, node]));
    const meshSets = new Map(), depths = new Map();
    function collect(id, depth) {
      const node = nodes.get(id);
      if (!node || depth > MAX_DEPTH || meshSets.has(id)) fail('模型部件层级无效。');
      const set = new Set(node.mesh_indices);
      depths.set(id, depth);
      for (const child of node.children) for (const mesh of collect(child, depth + 1)) set.add(mesh);
      meshSets.set(id, set);
      return set;
    }
    collect(assembly.root_id, 0);
    const meshOwners = new Map();
    for (const node of assembly.nodes) for (const mesh of node.mesh_indices) {
      const current = meshOwners.get(mesh);
      if (!current || depths.get(node.id) > depths.get(current)) meshOwners.set(mesh, node.id);
    }
    return {nodes, meshSets, meshOwners};
  }
  function viewState(value, nodes) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) fail('视图记录无效。');
    function valid(id) { return id === null || (typeof id === 'string' && nodes.has(id)); }
    if (!valid(value.selected_node_id) || !valid(value.isolated_node_id) ||
        !Array.isArray(value.hidden_node_ids) || value.hidden_node_ids.length > MAX_NODES ||
        value.hidden_node_ids.some(id => typeof id !== 'string' || !nodes.has(id)) ||
        new Set(value.hidden_node_ids).size !== value.hidden_node_ids.length) fail('视图记录引用了无效部件。');
    return {selected_node_id:value.selected_node_id, isolated_node_id:value.isolated_node_id,
      hidden_node_ids:[...value.hidden_node_ids]};
  }
  const api = {build, index, viewState};
  scope.CadAssemblyData = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
