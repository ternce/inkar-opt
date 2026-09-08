const BRANCH_SAP_CODES: Record<string, string> = {
  Алматы: '1001',
  Астана: '1002',
  Атырау: '1003',
  Есик: '1004',
  Караганда: '1005',
  Костанай: '1006',
  Семей: '1007',
  'Усть-Каменогорск': '1008',
  Павлодар: '1009',
  Актау: '1010',
  Актобе: '1011',
  Талдыкорган: '1012',
  Шымкент: '1013',
  Уральск: '1014',
};

export function previewGeneratedPriceFormatCode(priceListType: string, branch: string, sequence: string | number) {
  const sapBranchCode = BRANCH_SAP_CODES[branch] || 'SAP';
  const sequenceNumber = Number(sequence || 0);
  const paddedSequence = sequenceNumber > 0 ? String(sequenceNumber).padStart(3, '0') : 'NNN';
  return `${priceListType || 'ИПЛ'}_${sapBranchCode}_${paddedSequence}`;
}
