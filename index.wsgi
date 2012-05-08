import sae
from shell import app

application = sae.create_wsgi_app(app)
