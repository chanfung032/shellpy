#!/usr/bin/python
#
# Copyright 2007 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
An interactive, stateful AJAX shell that runs Python code on the server.

Part of http://code.google.com/p/google-app-engine-samples/.

May be run as a standalone app or in an existing app as an admin-only handler.
Can be used for system administration tasks, as an interactive way to try out
APIs, or as a debugging aid during development.

The logging, os, sys, db, and users modules are imported automatically.

Interpreter state is stored in the datastore so that variables, function
definitions, and other values in the global and local namespaces can be used
across commands.

To use the shell in your app, copy shell.py, static/*, and templates/* into
your app's source directory. Then, copy the URL handlers from app.yaml into
your app.yaml.

TODO: unit tests!
"""

import logging
import new
import os
import pickle
import sys
import traceback
import types
import MySQLdb
import tornado.web
import tornado.wsgi
import _mysql
import pylibmc
import sae.core
import sae.kvdb
from sae.const import (MYSQL_HOST, MYSQL_HOST_S,
    MYSQL_PORT, MYSQL_USER, MYSQL_PASS, MYSQL_DB
)


# Set to True if stack traces should be shown in the browser, etc.
_DEBUG = True

# The entity kind for shell sessions. Feel free to rename to suit your app.
_SESSION_KIND = '_Shell_Session'

# Types that can't be pickled.
UNPICKLABLE_TYPES = (
  types.ModuleType,
  types.TypeType,
  types.ClassType,
  types.FunctionType,
  pylibmc.Client,
  sae.kvdb.KVClient,
)

# Unpicklable statements to seed new sessions with.
INITIAL_UNPICKLABLES = [
  'import os',
  'import sys',
]

def _db():
  return _mysql.connect(host=MYSQL_HOST, port=int(MYSQL_PORT),
    user=MYSQL_USER, passwd=MYSQL_PASS, db=MYSQL_DB)
  #return _mysql.connect(user='root', passwd='root', db='test')


class Session:
  """A shell session. Stores the session's globals.

  Each session globals is stored in one of two places:

  If the global is picklable, it's stored in the globals.

  If the global is not picklable (e.g. modules, classes, and functions), or if
  it was created by the same statement that created an unpicklable global,
  it's not stored directly. Instead, the statement is stored in the
  unpicklables list property. On each request, before executing the current
  statement, the unpicklable statements are evaluated to recreate the
  unpicklable globals.

  The unpicklable_names property stores all of the names of globals that were
  added by unpicklable statements. When we pickle and store the globals after
  executing a statement, we skip the ones in unpicklable_names.
  """
  def __init__(self):
    self.globals = {}
    self.unpicklables = []

  @classmethod
  def get(cls, session_key):
    db = _db()
    db.query("""select * from sessions where id = %s""" % session_key)
    result = db.store_result()
    row = result.fetch_row(how=1)[0]
    db.close()
    
    session = cls()
    session.ID = row['id']
    session.globals = pickle.loads(row['globals'])
    session.unpicklables = pickle.loads(row['unpicklables'])
    return session

  def put(self):
    db = _db()
    db_globals = db.escape_string(pickle.dumps(self.globals))
    db_unpicklables = db.escape_string(pickle.dumps(self.unpicklables))
    if hasattr(self, 'ID'):
      db.query("""update sessions set globals='%s', unpicklables='%s'
                  where id=%s""" % (db_globals, db_unpicklables, self.ID))
    else:
      db.query("""insert into sessions(globals, unpicklables) values('%s', '%s')""" % 
        (db_globals, db_unpicklables))
      self.ID = db.insert_id()
    db.close()
    return self.ID

  def add_global(self, name, value):
    """Adds a global, or updates it if it already exists.

    Args:
      name: the name of the global to remove
      value: any picklable value
    """
    self.globals[name] = value

  def remove_global(self, name):
    """Removes a global, if it exists.

    Args:
      name: string, the name of the global to remove
    """
    if name in self.globals:
      del self.globals[name]

  def add_unpicklable(self, statement, names):
    """Adds a statement and list of names to the unpicklables.

    Args:
      statement: string, the statement that created new unpicklable global(s).
      names: list of strings; the names of the globals created by the statement.
    """
    self.unpicklables.append(statement)

    for k in names:
      if k in self.globals:
        del self.globals[k]


class FrontPageHandler(tornado.web.RequestHandler):
  """Creates a new session and renders the shell.html template.
  """

  def get(self):
    # set up the session. TODO: garbage collect old shell sessions
    session_key = self.get_argument('session', None)
    if session_key:
      session = Session.get(session_key)
    else:
      # create a new session
      session = Session()
      session.unpicklables = INITIAL_UNPICKLABLES
      session_key = session.put()

    template_file = os.path.join(os.path.dirname(__file__), 'templates',
                                 'shell.html')
    self.render(template_file, 
                session=str(session_key),
                python_version=sys.version)


class StatementHandler(tornado.web.RequestHandler):
  """Evaluates a python statement in a given session and returns the result.
  """

  def get(self):
    self.set_header('Content-Type', 'text/plain')

    # extract the statement to be run
    statement = self.get_argument('statement')
    if not statement:
      return

    # load the session from the datastore
    session = Session.get(self.get_argument('session'))
    print session.globals
    print session.unpicklables
    if session.globals.get('_debug', False):
      self.write("(%s-%d)\n" % (sae.core.environ['SERVER_ADDR'], os.getpid()))
      
    # the python compiler doesn't like network line endings
    statement = statement.replace('\r\n', '\n')

    # add a couple newlines at the end of the statement. this makes
    # single-line expressions such as 'class Foo: pass' evaluate happily.
    statement += '\n\n'

    # log and compile the statement up front
    try:
      logging.info('Compiling and evaluating:\n%s' % statement)
      compiled = compile(statement, '<string>', 'single')
    except:
      self.write(traceback.format_exc())
      return

    # create a dedicated module to be used as this statement's __main__
    statement_module = new.module('__main__')

    # use this request's __builtin__, since it changes on each request.
    # this is needed for import statements, among other things.
    import __builtin__
    statement_module.__builtins__ = __builtin__

    # swap in our custom module for __main__. then unpickle the session
    # globals, run the statement, and re-pickle the session globals, all
    # inside it.
    old_main = sys.modules.get('__main__')
    try:
      sys.modules['__main__'] = statement_module
      statement_module.__name__ = '__main__'
      statement_module.__file__ = __file__

      # re-evaluate the unpicklables
      for code in session.unpicklables:
        exec code in statement_module.__dict__

      # re-initialize the globals
      for name, val in session.globals.iteritems():
        try:
          statement_module.__dict__[name] = val
        except:
          msg = 'Dropping %s since it could not be unpickled.\n' % name
          self.write(msg)
          logging.warning(msg + traceback.format_exc())
          session.remove_global(name)

      # run!
      old_globals = dict(statement_module.__dict__)
      try:
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
          sys.stdout = self
          sys.stderr = self
          exec compiled in statement_module.__dict__
        finally:
          sys.stdout = old_stdout
          sys.stderr = old_stderr
      except:
        self.write(traceback.format_exc())
        return

      # extract the new globals that this statement added
      new_globals = {}
      for name, val in statement_module.__dict__.items():
        if name not in old_globals or val != old_globals[name]:
          new_globals[name] = val

      if True in [isinstance(val, UNPICKLABLE_TYPES)
                  for val in new_globals.values()]:
        # this statement added an unpicklable global. store the statement and
        # the names of all of the globals it added in the unpicklables.
        session.add_unpicklable(statement, new_globals.keys())
        logging.debug('Storing this statement as an unpicklable.')

      else:
        # this statement didn't add any unpicklables. pickle and store the
        # new globals back into the datastore.
        for name, val in new_globals.items():
          if not name.startswith('__'):
            session.add_global(name, val)

    finally:
      sys.modules['__main__'] = old_main

    session.put()


settings = {
  "static_path": os.path.join(os.path.dirname(__file__), "static"),
}

app = tornado.wsgi.WSGIApplication([
  (r"/", FrontPageHandler),
  (r"/shell.do", StatementHandler),
  (r"/shell.js", tornado.web.StaticFileHandler),
], **settings)

if __name__ == '__main__':
    import wsgiref.simple_server

    httpd = wsgiref.simple_server.make_server('', 8080, app)
    httpd.serve_forever()
