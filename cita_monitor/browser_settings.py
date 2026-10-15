"""Shared browser launch settings and narrowly scoped headless UA compatibility."""


def launch_options(config, *, headless=None):
    settings = config['browser']
    options = {'headless': settings['headless'] if headless is None else headless}
    if settings.get('channel'):
        options['channel'] = settings['channel']
    return options


def new_context(browser, config, *, headless=None):
    settings = config['browser']
    headless = settings['headless'] if headless is None else headless
    options = {'locale': 'es-ES'}
    if headless and settings.get('normalize_headless_user_agent', False):
        # Read the installed engine's actual UA. No hard-coded Chrome version,
        # OS/platform spoofing, webdriver patch or third-party stealth scripts.
        temporary = browser.new_context()
        try:
            page = temporary.new_page()
            user_agent = page.evaluate('navigator.userAgent')
        finally:
            temporary.close()
        options['user_agent'] = user_agent.replace('HeadlessChrome/', 'Chrome/')
    return browser.new_context(**options)
