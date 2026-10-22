"""Isolated browser contract tests with mocked API; not a real import/OCR test.

Can run directly using Python with playwright and jinja2 installed, independently
of the device/server dependency set. Also collected by the browser pytest suite.
"""
import copy
import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


def check_textbook_ui(width):
    from playwright.sync_api import sync_playwright

    ui = Path(__file__).resolve().parents[2] / 'lira' / 'ui'
    html = Environment(loader=FileSystemLoader(ui / 'templates')).get_template('textbooks.html').render()
    original = {
        'id': 'test-draft', 'status': 'review', 'revision': 1,
        'title': '二年级语文上册 <img src=x onerror=alert(1)>',
        'pages': [{'number': 1, 'text': '<script>bad()</script>'}],
        'warnings': ['核对字表 <img src=x onerror=alert(1)>'],
        'lessons': [{'number': 1, 'unit': 1, 'title': '小蝌蚪找妈妈', 'four': ['两'], 'two': ['塘'], 'source_pages': [1], 'reviewed': False}],
    }
    draft = copy.deepcopy(original)
    calls = []
    conflict = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel=os.environ.get('PLAYWRIGHT_CHANNEL') or None)
        page = browser.new_page(viewport={'width': width, 'height': 1000})

        def route(request):
            nonlocal draft
            url = request.request.url
            method = request.request.method
            if '/static/' in url:
                path = ui / 'static' / url.rsplit('/', 1)[1].split('?',1)[0]
                request.fulfill(body=path.read_text('utf-8'), content_type='text/css' if path.suffix == '.css' else 'application/javascript')
            elif url.endswith('/dev/textbooks'):
                request.fulfill(body=html, content_type='text/html')
            elif '/pages/' in url:
                request.fulfill(body='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800"><rect width="600" height="800" fill="white"/></svg>', content_type='image/svg+xml')
            elif url.endswith('/api/textbooks'):
                request.fulfill(json={'books': [], 'drafts': [{'id': draft['id'], 'title': draft['title'], 'status': draft['status'], 'page_count': 1}]})
            elif url.endswith('/publish'):
                calls.append(('publish', request.request.post_data_json))
                request.fulfill(json={'book': {'title': draft['title']}})
            elif '/drafts/' in url:
                if method == 'PUT':
                    calls.append(('save', request.request.post_data_json))
                    if conflict:
                        request.fulfill(status=409, json={'detail': 'conflict'})
                        return
                    draft.update(request.request.post_data_json)
                    draft['revision'] += 1
                request.fulfill(json={'draft': draft})
            else:
                request.fulfill(status=404)

        page.route('http://textbook.test/**', route)
        page.on('dialog', lambda dialog: dialog.accept())
        page.goto('http://textbook.test/dev/textbooks')
        page.locator('#library .book-card').click()
        page.get_by_role('tab',name='课文',exact=True).click()
        page.locator('#lessons .lesson').wait_for()
        assert page.locator('#warnings img').count() == 0
        assert page.locator('#publish').is_disabled()
        review = page.get_by_label('我已对照原页核对本课字表和分类')
        assert review.evaluate('input=>input.getBoundingClientRect().width')<=28
        assert page.locator('.review-check span').evaluate('span=>span.getBoundingClientRect().width')>100
        review.check()
        assert page.locator('#publish').is_enabled()
        page.get_by_label('四会字', exact=True).fill('两 就')
        assert not review.is_checked()
        assert page.locator('#publish').is_disabled()
        page.get_by_label('单元', exact=True).fill('')
        page.locator('#save').click()
        page.wait_for_function("document.querySelector('#notice').textContent.includes('草稿已保存')")
        assert calls[-1][1]['lessons'][0]['unit'] is None
        page.get_by_label('单元', exact=True).fill('1')
        review.check()
        page.locator('#save').click()
        page.wait_for_function("document.querySelector('#notice').textContent.includes('草稿已保存')")
        assert calls[-1][1]['lessons'][0]['four'] == ['两', '就']
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        # A revision conflict leaves the user's edits intact and does not publish.
        conflict = True
        page.get_by_label('课文标题', exact=True).fill('编辑仍保留')
        page.locator('#save').click()
        page.wait_for_function("document.querySelector('#notice').textContent.includes('其他操作更新')")
        assert page.get_by_label('课文标题', exact=True).input_value() == '编辑仍保留'
        assert not any(kind == 'publish' for kind, _ in calls)
        conflict = False
        review.check()
        page.get_by_role('tab',name='核对结果',exact=True).click()
        page.locator('#publish').click()
        page.wait_for_function("document.querySelector('#notice').textContent.includes('现在可以按课次')")
        assert calls[-1][0] == 'publish'
        assert calls[-1][1]['confirmed'] is True
        assert calls[-1][1]['revision'] == draft['revision']
        page.wait_for_function("document.querySelector('#lessons textarea').disabled")
        browser.close()


def test_textbooks_review_mobile():
    check_textbook_ui(390)


def test_textbooks_review_desktop():
    check_textbook_ui(1440)


if __name__ == '__main__':
    test_textbooks_review_mobile()
    test_textbooks_review_desktop()
    print('PASS: mobile and desktop mock-API textbook review flows')
