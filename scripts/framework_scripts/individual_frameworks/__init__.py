from .hexo import detect_hexo
from .jekyll import detect_jekyll
from .static_html import detect_static_html
from .react import detect_react
from .nextjs import detect_nextjs
from .express import detect_express
from .hugo import detect_hugo
from .vue import detect_vue
from .pelican import detect_pelican
from .quarto import detect_quarto
from .flask import detect_flask

# Order matters if you care about precedence.
DETECTORS = [
    ("Hexo", detect_hexo),
    ("Jekyll", detect_jekyll),
    ("Hugo", detect_hugo),
    ("React", detect_react),
    ("Next.js", detect_nextjs),
    ("Express", detect_express),
    ("Vue", detect_vue),
    ("Pelican", detect_pelican),
    ("Quarto", detect_quarto),
    ("Flask", detect_flask),
]
