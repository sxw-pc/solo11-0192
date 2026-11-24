"""Prepare grain shipment filing sections.

离线采录的每页原件都带编号与版本；整份运单以 *内容根* (content_root)
标识：根是全部页面规范化摘要的哈希，任一页改版都会换根。页面签名把
页内容绑定到该根，监管端回执同样带内容根并由监管端密钥签名，因此
“同一 manifest_id 的三张页”只有在同根同版时才算同一份运单。
"""
import hashlib
import hmac
import json

DEFAULT_DOCUMENT_SECRET = 'grainrelease-document-hmac-key'
REQUIRED_KINDS = ('header', 'load', 'seal')


def canonical(value):
    """Deterministic JSON serialization used for every digest and signature."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sha256_hex(value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def page_digest(page):
    """Digest of a page's own content (number, kind, version and payload)."""
    body = {key: page[key] for key in ('number', 'kind', 'version') if key in page}
    if 'payload' in page:
        body['payload'] = page['payload']
    return sha256_hex(canonical(body))


def content_root(manifest_id, pages):
    """Content root over every page digest; changes whenever any page changes."""
    if not pages:
        raise ValueError('运单至少需要一页')
    ordered = sorted(pages, key=lambda p: p['number'])
    numbers = [p['number'] for p in ordered]
    if len(set(numbers)) != len(numbers):
        raise ValueError('页编号重复，无法形成内容根')
    material = {
        'manifest_id': manifest_id,
        'pages': [
            {'number': p['number'], 'kind': p['kind'], 'version': p['version'], 'digest': page_digest(p)}
            for p in ordered
        ],
    }
    return sha256_hex(canonical(material))


def sign_page(manifest_id, page, root, secret=DEFAULT_DOCUMENT_SECRET):
    material = {'manifest_id': manifest_id, 'content_root': root,
                'number': page['number'], 'kind': page['kind'], 'version': page['version'],
                'digest': page_digest(page)}
    return hmac.new(secret.encode('utf-8'), canonical(material), hashlib.sha256).hexdigest()


def sign_document(document, secret=DEFAULT_DOCUMENT_SECRET):
    """Return a new document whose pages carry content_root and valid signatures."""
    manifest_id = document['manifest_id']
    source_pages = document['pages']
    root = content_root(manifest_id, source_pages)
    pages = []
    for page in sorted(source_pages, key=lambda p: p['number']):
        signed = dict(page)
        signed['content_root'] = root
        signed['signature'] = sign_page(manifest_id, page, root, secret)
        pages.append(signed)
    return {'manifest_id': manifest_id, 'pages': pages, 'content_root': root}


def verify_document(document, secret=DEFAULT_DOCUMENT_SECRET):
    """Validate page numbers, versions, content-root binding and page signatures."""
    manifest_id = document.get('manifest_id')
    if not manifest_id:
        raise ValueError('缺少 manifest_id')
    pages = document.get('pages')
    if not pages:
        raise ValueError('运单缺少页面')
    numbers = [p.get('number') for p in pages]
    if len(numbers) != len(set(numbers)):
        raise ValueError('页编号重复')
    expected_root = document.get('content_root') or content_root(manifest_id, pages)
    if content_root(manifest_id, pages) != expected_root:
        raise ValueError('内容根与页面摘要不一致，页面被篡改')
    for page in pages:
        if not isinstance(page.get('version'), int) or page['version'] < 1:
            raise ValueError('页版本必须为正整数')
        if page.get('content_root') != expected_root:
            raise ValueError('页面绑定的内容根不一致，存在混入版本')
        signature = page.get('signature')
        if not signature:
            raise ValueError('页面缺少签名')
        expected = sign_page(manifest_id, page, expected_root, secret)
        if not hmac.compare_digest(signature, expected):
            raise ValueError('页面验签失败')
    return expected_root


def prepare(document, secret=DEFAULT_DOCUMENT_SECRET):
    """Verify and prepare a manifest; still returns manifest_id and pages."""
    root = verify_document(document, secret)
    return {'manifest_id': document['manifest_id'], 'pages': document['pages'], 'content_root': root}
