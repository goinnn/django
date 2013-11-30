# Wrapper for loading templates from storage of some sort (e.g. filesystem, database).
#
# This uses the TEMPLATE_LOADERS setting, which is a list of loaders to use.
# Each loader is expected to have this interface:
#
#    callable(name, dirs=[])
#
# name is the template name.
# dirs is an optional list of directories to search instead of TEMPLATE_DIRS.
#
# The loader should return a tuple of (template_source, path). The path returned
# might be shown to the user for debugging purposes, so it should identify where
# the template was loaded from.
#
# A loader may return an already-compiled template instead of the actual
# template source. In that case the path returned should be None, since the
# path information is associated with the template during the compilation,
# which has already been done.
#
# Each loader should have an "is_usable" attribute set. This is a boolean that
# specifies whether the loader can be used in this Python installation. Each
# loader is responsible for setting this when it's initialized.
#
# For example, the eggs loader (which is capable of loading templates from
# Python eggs) sets is_usable to False if the "pkg_resources" module isn't
# installed, because pkg_resources is necessary to read eggs.
import warnings

from django.core.exceptions import ImproperlyConfigured
from django.template.base import Origin, Template, Context, TemplateDoesNotExist, add_to_builtins
from django.conf import settings
from django.utils.module_loading import import_by_path
from django.utils import six

template_source_loaders = None


def get_template_source_loaders():
    """
    Calculate template_source_loaders the first time the function is executed
    because putting this logic in the module-level namespace may cause
    circular import errors. See Django ticket #1292.
    """
    global template_source_loaders
    if template_source_loaders is None:
        loaders = []
        for loader_name in settings.TEMPLATE_LOADERS:
            loader = find_template_loader(loader_name)
            if loader is not None:
                loaders.append(loader)
        template_source_loaders = tuple(loaders)
    return template_source_loaders


class BaseLoader(object):
    is_usable = False
    use_skip_template = False
    never_skip = False

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, template_name, template_dirs=None, skip_template=None):
        return self.load_template(template_name, template_dirs, skip_template)

    def load_template(self, template_name, template_dirs=None, skip_template=None):
        source, display_name = self.load_template_source(template_name, template_dirs)
        origin = make_origin(display_name, self.load_template_source, template_name, template_dirs)
        try:
            template = get_template_from_string(source, origin, template_name)
            return template, None
        except TemplateDoesNotExist:
            # If compiling the template we found raises TemplateDoesNotExist, back off to
            # returning the source and display name for the template we were asked to load.
            # This allows for correct identification (later) of the actual template that does
            # not exist.
            return source, display_name

    def load_template_source(self, template_name, template_dirs=None):
        """
        Returns a tuple containing the source and origin for the given template
        name.

        """
        raise NotImplementedError('subclasses of BaseLoader must provide a '
                                  'load_template_source() method')

    def reset(self):
        """
        Resets any state maintained by the loader instance (e.g., cached
        templates or cached loader modules).

        """
        pass


class LoaderOrigin(Origin):
    def __init__(self, display_name, loader, name, dirs):
        super(LoaderOrigin, self).__init__(display_name)
        self.loader, self.loadname, self.dirs = loader, name, dirs

    def reload(self):
        return self.loader(self.loadname, self.dirs)[0]


def make_origin(display_name, loader, name, dirs):
    if display_name:
        return LoaderOrigin(display_name, loader, name, dirs)
    else:
        return None


def find_template_loader(loader):
    if isinstance(loader, (tuple, list)):
        loader, args = loader[0], loader[1:]
    else:
        args = []
    if isinstance(loader, six.string_types):
        TemplateLoader = import_by_path(loader)

        if hasattr(TemplateLoader, 'load_template_source'):
            func = TemplateLoader(*args)
        else:
            # Try loading module the old way - string is full path to callable
            if args:
                raise ImproperlyConfigured("Error importing template source "
                                           "loader %s - can't pass arguments "
                                           "to function-based loader." % loader)
            func = TemplateLoader

        if not func.is_usable:
            warnings.warn("Your TEMPLATE_LOADERS setting includes %r, but your "
                          "Python installation doesn't support that type of "
                          "template loading. Consider removing that line from "
                          "TEMPLATE_LOADERS." % loader)
            return None
        else:
            return func
    else:
        raise ImproperlyConfigured('Loader does not define a "load_template" '
                                   'callable template source loader')


def find_template(name, dirs=None, skip_template=None, loaders=None):
    """
    Returns a tuple with a compiled Template object for the given template name
    and an origin object. If ``loaders`` is given, only the specified loaders will be
    tried. If ``skip_template`` is given, the loader of the specified template will be
    skipped (provided that it doesn't have a ``never_skip`` attribute set to True).
    """
    loaders = get_template_source_loaders() if loaders is None else loaders
    # If there is a template to skip with the same name as the current template,
    # skip all the loaders until and including the loader of the template to
    # be skipped
    if getattr(skip_template, 'loadname', None) == name:
        loaders = skip_loaders(loaders, skip_template)
    for loader in loaders:
        try:
            source, display_name = loader(name, dirs, skip_template)
            return (source, make_origin(display_name, loader, name, dirs))
        except TemplateDoesNotExist:
            pass
    raise TemplateDoesNotExist(name)


def skip_loaders(loaders, skip_template):
    """
    Skips all the loaders until and including the loader of `skip_template`.
    """
    # Get the loader object to skip
    loader_to_skip = getattr(skip_template, 'loader', None)
    loader_to_skip = getattr(loader_to_skip, '__self__', loader_to_skip)
    has_been_skiped = False
    for loader in loaders:
        # Get the current loader object
        loader = getattr(loader, '__self__', loader)
        if has_been_skiped or loader.never_skip:
            yield loader
        if loader == loader_to_skip:
            has_been_skiped = True


def get_template(template_name, dirs=None, skip_template=None):
    """
    Returns a compiled Template object for the given template name, handling
    template inheritance recursively.
    """
    template, origin = find_template(template_name, dirs, skip_template)
    if not hasattr(template, 'render'):
        # template needs to be compiled
        template = get_template_from_string(template, origin, template_name)
    return template


def get_template_from_string(source, origin=None, name=None):
    """
    Returns a compiled Template object for the given template code,
    handling template inheritance recursively.
    """
    return Template(source, origin, name)


def render_to_string(template_name, dictionary=None, context_instance=None,
                     dirs=None):
    """
    Loads the given template_name and renders it with the given dictionary as
    context. The template_name may be a string to load a single template using
    get_template, or it may be a tuple to use select_template to find one of
    the templates in the list. Returns a string.
    """
    dictionary = dictionary or {}
    if isinstance(template_name, (list, tuple)):
        t = select_template(template_name, dirs)
    else:
        t = get_template(template_name, dirs)
    if not context_instance:
        return t.render(Context(dictionary))
    # Add the dictionary to the context stack, ensuring it gets removed again
    # to keep the context_instance in the same state it started in.
    with context_instance.push(dictionary):
        return t.render(context_instance)


def select_template(template_name_list, dirs=None, skip_template=None):
    "Given a list of template names, returns the first that can be loaded."
    if not template_name_list:
        raise TemplateDoesNotExist("No template names provided")
    not_found = []
    for template_name in template_name_list:
        try:
            return get_template(template_name, dirs, skip_template)
        except TemplateDoesNotExist as e:
            if e.args[0] not in not_found:
                not_found.append(e.args[0])
            continue
    # If we get here, none of the templates could be loaded
    raise TemplateDoesNotExist(', '.join(not_found))

add_to_builtins('django.template.loader_tags')
