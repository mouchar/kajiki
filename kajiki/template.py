# -*- coding: utf-8 -*-

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import re
import types
from nine import IS_PYTHON2, basestring, str, iteritems

try:
    from functools import update_wrapper
except:
    def update_wrapper(wrapper, wrapped,
                       assigned=('__module__', '__name__', '__doc__'),
                       updated = ('__dict__',)):
        for attr in assigned:
            setattr(wrapper, attr, getattr(wrapped, attr))
        for attr in updated:
            getattr(wrapper, attr).update(getattr(wrapped, attr))
        return wrapper

import kajiki
from .util import flattener, literal
from .html_utils import HTML_EMPTY_ATTRS
from .ir import generate_python
from . import lnotab
from kajiki import i18n


class _obj(object):
    def __init__(self, **kw):
        for k, v in iteritems(kw):
            setattr(self, k, v)


class _Template(object):
    """Base Class for all compiled Kajiki Templates.

    All kajiki templates created from an ``ir.TemplateNode`` will
    be subclasses of this class.

    As the template body code runs inside ``__main__`` method of this
    class, the instance of this class is always available as ``self``
    inside the template code.

    This class also makes available some global object inside the
    template code itself:

        - ``local`` which is the instance of the template
        - ``defined`` which checks if the given variable is defined
          inside the template scope.
        - ``Markup`` which marks the passed object as markup code and
          prevents escaping for its content.
        - ``__kj__`` which is a special object used by generated code
          providing features like keeping track of py:with stack or
          or the gettext function used to translate text.
    """
    __methods__ = ()
    loader = None
    base_globals = None
    filename = None

    def __init__(self, context=None):
        if context is None:
            context = {}
        self._context = context
        base_globals = self.base_globals or {}
        self.__globals__ = dict(base_globals, local=self, self=self,
            defined=lambda x: x in self.__globals__,
            literal=literal, Markup=literal,
            __builtins__=__builtins__, __kj__=kajiki)
        self.__globals__['value_of'] = self.__globals__.get
        for k, v in self.__methods__:
            v = v.bind_instance(self)
            setattr(self, k, v)
            self.__globals__[k] = v
        self.__kj__ = _obj(
            extend=self._extend,
            push_switch=self._push_switch,
            pop_switch=self._pop_switch,
            case=self._case,
            import_=self._import,
            escape=self._escape,
            gettext=i18n.gettext,
            render_attrs=self._render_attrs,
            push_with=self._push_with,
            pop_with=self._pop_with,
            collect=self._collect,
        )
        self._switch_stack = []
        self._with_stack = []
        self.__globals__.update(context)

    def __iter__(self):
        '''We convert the chunk to string because it can be of any type
        -- after all, the template supports expressions such as ${x+y}.
        Here, ``chunk`` can be the computed expression result.
        '''
        for chunk in self.__main__():
            yield str(chunk)

    def render(self):
        return ''.join(self)

    def _push_with(self, locals_, vars):
        self._with_stack.append([locals_.get(k, ()) for k in vars])

    def _pop_with(self):
        return self._with_stack.pop()

    def _extend(self, parent):
        if isinstance(parent, basestring):
            parent = self.loader.import_(parent)
        p_inst = parent(self._context)
        p_globals = p_inst.__globals__
        # Find overrides
        for k, v in iteritems(self.__globals__):
            if k == '__main__':
                continue
            if not isinstance(v, TplFunc):
                continue
            p_globals[k] = v
        # Find inherited funcs
        for k, v in iteritems(p_inst.__globals__):
            if k == '__main__':
                continue
            if not isinstance(v, TplFunc):
                continue
            if k not in self.__globals__:
                self.__globals__[k] = v
            if not hasattr(self, k):
                def _(k=k):
                    '''Capture the 'k' variable in a closure'''
                    def trampoline(*a, **kw):
                        global parent
                        return getattr(parent, k)(*a, **kw)
                    return trampoline
                setattr(self, k, TplFunc(_()).bind_instance(self))
        p_globals['child'] = self
        p_globals['local'] = p_inst
        p_globals['self'] = self.__globals__['self']
        self.__globals__['parent'] = p_inst
        self.__globals__['local'] = self
        return p_inst

    def _push_switch(self, expr):
        self._switch_stack.append(expr)

    def _pop_switch(self):
        self._switch_stack.pop()

    def _case(self, obj):
        return obj == self._switch_stack[-1]

    def _import(self, name, alias, gbls):
        tpl_cls = self.loader.import_(name)
        if alias is None:
            alias = self.loader.default_alias_for(name)
        r = gbls[alias] = tpl_cls(gbls)
        return r

    def _escape(self, value):
        "Returns the given HTML with ampersands, carets and quotes encoded."
        if value is None or isinstance(value, flattener):
            return value
        if hasattr(value, '__html__'):
            return value.__html__()
        uval = str(value)
        if self._re_escape.search(uval):  # Scan the string before working.
            # stdlib escape() is inconsistent between Python 2 and Python 3.
            # In 3, html.escape() translates the single quote to '&#39;'
            # In 2.6 and 2.7, cgi.escape() does not touch the single quote.
            # Preserve our tests and Kajiki behaviour across Python versions:
            return uval.replace('&', '&amp;').replace('<', '&lt;') \
                .replace('>', '&gt;').replace('"', '&quot;')
            # .replace("'", '&#39;'))
            # Above we do NOT escape the single quote; we don't need it because
            # all HTML attributes are double-quoted in our output.
        else:
            return uval
    _re_escape = re.compile(r'&|<|>|"')

    def _render_attrs(self, attrs, mode):
        if hasattr(attrs, 'items'):
            attrs = attrs.items()
        if attrs is not None:
            for k, v in sorted(attrs):
                if k in HTML_EMPTY_ATTRS and v in (True, False):
                    v = k if v else None
                if v is None:
                    continue
                if mode.startswith('html') and k in HTML_EMPTY_ATTRS:
                    yield ' ' + k.lower()
                else:
                    yield ' %s="%s"' % (k, self._escape(v))

    def _collect(self, it):
        result = []
        for part in it:
            if part is None:
                continue
            result.append(str(part))
        if result:
            return ''.join(result)
        else:
            return None

    @classmethod
    def annotate_lnotab(cls, py_to_tpl):
        for name, meth in cls.__methods__:
            meth.annotate_lnotab(cls.filename, py_to_tpl, dict(py_to_tpl))

    def defined(self, name):
        return name in self._context


def Template(ns):
    """Creates a new ``_Template`` subclass from an entity with ``exposed`` functions.

    Kajiki used classes as containers of the exposed functions for convenience,
    but any object that can have the functions as attributes works.

    To be a valid template the original entity must provide at least a ``__main__``
    function::

        class Example:
            @kajiki.expose
            def __main__():
                yield 'Hi'

        t = kajiki.Template(Example)
        output = t().render()

        print(output)
        'Hi'
    """
    dct = {}
    methods = dct['__methods__'] = []
    for name in dir(ns):
        value = getattr(ns, name)
        if getattr(value, 'exposed', False):
            methods.append((name, TplFunc(getattr(value, '__func__', value))))
    return type(ns.__name__, (_Template,), dct)


def from_ir(ir_node):
    """Creates a template class from Intermediate Representation TemplateNode.

    This actually creates the class defined by the TemplateNode and returns
    a subclass of it.
    The returned class is a subclass of a `kajiki.template._Template`.
    """
    py_lines = list(generate_python(ir_node))
    py_text = '\n'.join(map(str, py_lines))
    py_linenos = []
    last_lineno = 0
    for i, l in enumerate(py_lines):
        lno = max(last_lineno, l._lineno or 0)
        py_linenos.append((i + 1, lno))
        last_lineno = lno
    dct = dict(kajiki=kajiki)
    try:
        exec(py_text, dct)
    except (SyntaxError, IndentationError) as e:  # pragma no cover
        raise KajikiSyntaxError(e.msg, py_text, e.filename, e.lineno, e.offset)
    tpl = dct['template']
    tpl.base_globals = dct
    tpl.py_text = py_text
    tpl.filename = ir_node.filename
    tpl.annotate_lnotab(py_linenos)
    return tpl


class TplFunc(object):
    def __init__(self, func, inst=None):
        self._func = func
        self._inst = inst
        self._bound_func = None

    def bind_instance(self, inst):
        return TplFunc(self._func, inst)

    def __repr__(self):  # pragma no cover
        if self._inst:
            return '<bound tpl_function %r of %r>' % (
                self._func.__name__, self._inst)
        else:
            return '<unbound tpl_function %r>' % (self._func.__name__)

    def __call__(self, *args, **kwargs):
        if self._bound_func is None:
            self._bound_func = self._bind_globals(
                self._inst.__globals__)
        return self._bound_func(*args, **kwargs)

    def _bind_globals(self, globals):
        '''Return a function which has the globals dict set to 'globals'
        and which flattens the result of self._func'.
        '''
        func = types.FunctionType(
            self._func.__code__,
            globals,
            self._func.__name__,
            self._func.__defaults__,
            self._func.__closure__
        )
        return update_wrapper(
            lambda *a, **kw: flattener(func(*a, **kw)),
            func)

    def annotate_lnotab(self, filename, py_to_tpl, py_to_tpl_dct):
        if not py_to_tpl:
            return
        code = self._func.__code__
        new_lnotab_numbers = []
        for bc_off, py_lno in lnotab.lnotab_numbers(
                code.co_lnotab, code.co_firstlineno):
            tpl_lno = py_to_tpl_dct.get(py_lno, None)
            if tpl_lno is None:
                print('ERROR LOOKING UP LINE #%d' % py_lno)
                continue
            new_lnotab_numbers.append((bc_off, tpl_lno))
        if not new_lnotab_numbers:
            return
        new_firstlineno = py_to_tpl_dct.get(code.co_firstlineno, 0)
        new_lnotab = lnotab.lnotab_string(new_lnotab_numbers, new_firstlineno)
        new_code = patch_code_file_lines(
            code, filename, new_firstlineno, new_lnotab)
        self._func.__code__ = new_code
        return


if IS_PYTHON2:
    def patch_code_file_lines(code, filename, firstlineno, lnotab):
        return types.CodeType(code.co_argcount,
                              code.co_nlocals,
                              code.co_stacksize,
                              code.co_flags,
                              code.co_code,
                              code.co_consts,
                              code.co_names,
                              code.co_varnames,
                              filename.encode('utf-8'),
                              code.co_name,
                              firstlineno,
                              lnotab,
                              code.co_freevars,
                              code.co_cellvars)
else:
    def patch_code_file_lines(code, filename, firstlineno, lnotab):
        return types.CodeType(code.co_argcount,
                            code.co_kwonlyargcount,
                            code.co_nlocals,
                            code.co_stacksize,
                            code.co_flags,
                            code.co_code,
                            code.co_consts,
                            code.co_names,
                            code.co_varnames,
                            filename,
                            code.co_name,
                            firstlineno,
                            lnotab,
                            code.co_freevars,
                            code.co_cellvars)


class KajikiSyntaxError(Exception):
    def __init__(self, msg, source, filename, linen, coln):
        super(KajikiSyntaxError, self).__init__(
            '[%s:%s] %s\n%s' % (filename, linen, msg, self._get_source_snippet(source, linen))
        )
        self.filename = filename
        self.linenum = linen
        self.colnum = coln

    def _get_source_snippet(self, source, linen):
        SURROUNDING = 2
        linen -= 1

        parts = []
        for i in range(SURROUNDING, 0, -1):
            parts.append('\t     %s\n' % self._get_source_line(source, linen - i))
        parts.append('\t --> %s\n' % self._get_source_line(source, linen))
        for i in range(1, SURROUNDING+1):
            parts.append('\t     %s\n' % self._get_source_line(source, linen + i))
        return ''.join(parts)

    def _get_source_line(self, source, linen):
        if linen < 0:
            return ''

        try:
            return source.splitlines()[linen]
        except:
            return ''