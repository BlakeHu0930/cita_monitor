"""Small, non-executing parser for the site's server-rendered HTML forms."""
from dataclasses import dataclass, field
from html.parser import HTMLParser


class FormMismatch(ValueError):
    pass


@dataclass
class Element:
    tag: str
    attrs: dict
    text: str = ''
    options: list = field(default_factory=list)


@dataclass
class Form:
    attrs: dict
    controls: list = field(default_factory=list)

    def control(self, element_id):
        matches = [c for c in self.controls if c.attrs.get('id') == element_id]
        if len(matches) != 1:
            raise FormMismatch('查询表单字段发生变化')
        return matches[0]

    def fields(self, overrides=None):
        overrides = overrides or {}
        pairs = []
        found = set()
        for c in self.controls:
            a = c.attrs
            name = a.get('name')
            if not name or 'disabled' in a:
                continue
            kind = a.get('type', 'text').lower()
            if c.tag == 'button' or kind in ('button', 'submit', 'reset', 'image', 'file'):
                continue
            if name in overrides:
                if name not in found:
                    pairs.append((name, overrides[name]))
                    found.add(name)
                continue
            if kind in ('checkbox', 'radio') and 'checked' not in a:
                continue
            if c.tag == 'select':
                options = [o for o in c.options if 'disabled' not in o.attrs]
                selected = [o for o in options if 'selected' in o.attrs]
                if not selected and 'multiple' not in a:
                    selected = options[:1]
                pairs.extend((name, o.attrs.get('value', o.text.strip())) for o in selected)
            else:
                pairs.append((name, c.text if c.tag == 'textarea' else a.get('value', 'on' if kind in ('radio', 'checkbox') else '')))
        if found != set(overrides):
            raise FormMismatch('待填写字段不在当前查询表单中')
        return pairs

    def option(self, element_id, *, value=None, label=None):
        c = self.control(element_id)
        candidates = [o for o in c.options if 'disabled' not in o.attrs and
                      ((value is not None and o.attrs.get('value', o.text.strip()) == value) or
                       (label is not None and ' '.join(o.text.split()) == ' '.join(label.split())))]
        if len(candidates) != 1:
            raise FormMismatch('配置的选项不在官网表单中')
        return c.attrs['name'], candidates[0].attrs.get('value', candidates[0].text.strip())


class Document(HTMLParser):
    def __init__(self, html, url):
        super().__init__(convert_charrefs=True)
        self.html, self.url = html, url
        self.forms, self.elements, self.parts = [], [], []
        self.form = self.select = self.option_node = self.text_control = None
        self.skip = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style', 'head', 'noscript'):
            self.skip += 1
        if self.skip:
            return
        e = Element(tag, attrs)
        self.elements.append(e)
        if tag == 'form':
            self.form = Form(attrs)
            self.forms.append(self.form)
        elif tag in ('input', 'select', 'textarea', 'button') and self.form:
            self.form.controls.append(e)
            if tag == 'select':
                self.select = e
            if tag in ('textarea', 'button'):
                self.text_control = e
        elif tag == 'option' and self.select:
            self.option_node = e
            self.select.options.append(e)
        if tag in ('p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head', 'noscript') and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag == 'form':
            self.form = None
        elif tag == 'select':
            self.select = self.option_node = None
        elif tag == 'option':
            self.option_node = None
        elif tag in ('button', 'textarea'):
            self.text_control = None
        if tag in ('p', 'div', 'li', 'tr', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_data(self, data):
        if self.skip:
            return
        self.parts.append(data)
        if self.option_node:
            self.option_node.text += data
        if self.text_control:
            self.text_control.text += data

    @property
    def text(self):
        return ''.join(self.parts)

    def has(self, element_id):
        return any(e.attrs.get('id') == element_id for e in self.elements)

    def form_with(self, element_id):
        matches = [f for f in self.forms if any(c.attrs.get('id') == element_id for c in f.controls)]
        if len(matches) != 1:
            raise FormMismatch('预期的查询表单不存在或不唯一')
        return matches[0]
