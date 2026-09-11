export type ProductCatalogMappingRow = {
  productId?: number | null;
  ourProductId?: number | null;
  status?: string;
  platform?: string;
};

export type ProductCatalogMappingCandidate = {
  platform?: string;
  itemId?: number | null;
  sourceExternalKey?: string | null;
  sourceMatchKey?: string | null;
  sourceName?: string;
  sourceManufacturer?: string;
  sourceDosageForm?: string;
  sourceNormalizedName?: string;
  confidence?: number | null;
};

export const productCatalogSelectedProductId = (row?: ProductCatalogMappingRow | null) =>
  row?.productId || row?.ourProductId || null;

export const canConfirmProductCatalogMapping = (
  row: ProductCatalogMappingRow | null | undefined,
  candidate: ProductCatalogMappingCandidate | null | undefined,
  isLoading = false,
) => Boolean(!isLoading && row?.status !== 'rejected' && productCatalogSelectedProductId(row) && candidate?.sourceMatchKey);

export const buildProductCatalogMappingPayload = (
  row: ProductCatalogMappingRow,
  candidate: ProductCatalogMappingCandidate,
  fallbackPlatform: 'provisor' | 'vidman',
) => {
  const productId = productCatalogSelectedProductId(row);
  if (!productId || !candidate.sourceMatchKey) return null;
  const platform = candidate.platform === 'vidman' || candidate.platform === 'provisor' ? candidate.platform : fallbackPlatform;
  return {
    platform,
    status: 'mapped',
    itemId: candidate.itemId,
    sourceExternalKey: candidate.sourceExternalKey,
    sourceMatchKey: candidate.sourceMatchKey,
    sourceName: candidate.sourceName,
    sourceManufacturer: candidate.sourceManufacturer,
    sourceDosageForm: candidate.sourceDosageForm,
    sourceNormalizedName: candidate.sourceNormalizedName,
    ourProductId: productId,
    confidence: candidate.confidence || 100,
  };
};

export const buildProductCatalogCandidateUrl = (
  productId: number,
  platform: string,
  formatCode = '',
  useCurrentFormat = false,
) => {
  const params = new URLSearchParams({
    platform,
    limit: '5',
  });
  if (useCurrentFormat) params.set('format_code', formatCode);
  return `/api/competitors/code-mappings/product-catalog/${productId}/candidates?${params.toString()}`;
};
