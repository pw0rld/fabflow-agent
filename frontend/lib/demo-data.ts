/** Built-in, explicitly fictional data. Never use these assumptions as parsed evidence. */
export type MockPartIdentity = {
  id: string
  name: string | null
  code: string | null
  version: string | null
  material: string | null
  thickness: string | null
  size: string | null
}

export type DemoPart = MockPartIdentity & {
  name: string
  code: string
  version: string
  material: string
  thickness: string
  size: string
  setQty: number
  requirement: string
  kind: 'plate' | 'bracket' | 'case'
}

export const DEMO_PARTS: DemoPart[] = [
  { id: 'plate', name: '安装底板', code: 'PL-2401', version: 'V2.1', material: 'SPCC 冷轧钢板', thickness: '2 mm', size: '240 × 180 mm', setQty: 1, requirement: '演示标注：Ø12 × 4、Ø6 × 4；去除毛刺', kind: 'plate' },
  { id: 'bracket', name: 'L 型支架', code: 'BR-0816', version: 'V1.0', material: 'SUS304 不锈钢', thickness: '1.5 mm', size: '80 × 60 × 35 mm', setQty: 2, requirement: '演示标注：折弯角 90° ±1°；锐边倒钝', kind: 'bracket' },
  { id: 'case', name: '机箱底壳', code: 'EN-4200', version: 'V3.2', material: 'SECC 电解板', thickness: '1.2 mm', size: '420 × 300 × 160 mm', setQty: 1, requirement: '演示标注：箱体对角线差 ≤ 1 mm', kind: 'case' },
  { id: 'cover', name: '机箱上盖', code: 'EN-4201', version: 'V3.2', material: 'SECC 电解板', thickness: '1.2 mm', size: '420 × 300 × 28 mm', setQty: 1, requirement: '演示标注：与底壳配合间隙 0.5 mm', kind: 'case' },
]

export type MockAssumption = {
  material: string
  thicknessMm: number
  densityKgM3: number
  flatAreaM2: number
  perSet: number
  materialRateYuanKg: number
  processFeeCentsPiece: number
  preparationFeeCentsPiece: number
  process: string
}

/** Simulation profiles are indexed independently of the AI's geometry/material output. */
export const MOCK_ASSUMPTIONS: MockAssumption[] = [
  { material: 'SPCC（模拟）', thicknessMm: 2, densityKgM3: 7850, flatAreaM2: 0.0432, perSet: 1, materialRateYuanKg: 8.4, processFeeCentsPiece: 1600, preparationFeeCentsPiece: 200, process: '模拟：激光下料、去毛刺' },
  { material: 'SUS304（模拟）', thicknessMm: 1.5, densityKgM3: 7930, flatAreaM2: 0.012, perSet: 2, materialRateYuanKg: 22, processFeeCentsPiece: 1400, preparationFeeCentsPiece: 150, process: '模拟：激光下料、折弯、去毛刺' },
  { material: 'SECC（模拟）', thicknessMm: 1.2, densityKgM3: 7850, flatAreaM2: 0.32, perSet: 1, materialRateYuanKg: 7.8, processFeeCentsPiece: 4800, preparationFeeCentsPiece: 400, process: '模拟：下料、折弯、焊接' },
  { material: 'SECC（模拟）', thicknessMm: 1.2, densityKgM3: 7850, flatAreaM2: 0.14, perSet: 1, materialRateYuanKg: 7.8, processFeeCentsPiece: 2800, preparationFeeCentsPiece: 250, process: '模拟：下料、折弯、去毛刺' },
]
