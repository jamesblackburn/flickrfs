#!/usr/bin/python
#===============================================================================
#  flickrfs - Virtual Filesystem for Flickr
#  Copyright (c) 2005,2006 Manish Rai Jain  <manishrjain@gmail.com>
#
#  This program can be distributed under the terms of the GNU GPL version 2, or 
#  its later versions. 
#
# DISCLAIMER: The API Key and Shared Secret are provided by the author in 
# the hope that it will prevent unnecessary trouble to the end-user. The 
# author will not be liable for any misuse of this API Key/Shared Secret 
# through this application/derived apps/any 3rd party apps using this key. 
#===============================================================================

__author__ =  "Manish Rai Jain (manishrjain@gmail.com)"
__license__ = "GPLv2 (details at http://www.gnu.org/licenses/licenses.html#GPL)"

import thread, string, ConfigParser, mimetypes, codecs
import time, logging, logging.handlers, os, sys
from glob import glob
from errno import *
from traceback import format_exc
# The python-fuse api has changed in 0.2, but we're still using the 0.1 api.
# The following two lines line will make us compatible with both versions by
# making 2.0 use the 1.0 api.
# (See http://fuse4bsd.creo.hu/README.new_fusepy_api.html)
import fuse
fuse.fuse_python_api = (0, 1)
Fuse = fuse.Fuse
import threading
import random, commands
from urllib2 import URLError
from transactions import TransFlickr
import inodes

#Some global definitions and functions
NUMRETRIES = 3
DEFAULTCONFIG = """\
[configuration]

browser: /usr/bin/x-www-browser
image.size:
sets.sync.int: 300
stream.sync.int: 300
"""

#Set up the .flickfs directory.
homedir = os.getenv('HOME')
flickrfsHome = os.path.join(homedir, '.flickrfs')
dbPath = os.path.join(flickrfsHome, '.inode.bdb')

if not os.path.exists(flickrfsHome):
  os.mkdir(os.path.join(flickrfsHome))
else:
  # Remove previous metadata files from ~/.flickrfs
  for a in glob(os.path.join(flickrfsHome, '.*')):
    os.remove(os.path.join(flickrfsHome, a))
  try:
    os.remove(dbPath)
  except:
    pass
 
# Added by Varun Hiremath
if not os.path.exists(flickrfsHome + "/config.txt"):
  fconfig = open(flickrfsHome+"/config.txt",'w')
  fconfig.write(DEFAULTCONFIG)
  fconfig.close()

# Set up logging
rootlogger = logging.getLogger()
loghdlr = logging.handlers.RotatingFileHandler(
                             os.path.join(flickrfsHome,'log'), "a", 5242880, 3)
logfmt = logging.Formatter("%(asctime)s %(name)-14s %(levelname)-7s %(threadName)-10s %(funcName)-22s %(message)s", "%x %X")
loghdlr.setFormatter(logfmt)
rootlogger.addHandler(loghdlr)
rootlogger.setLevel(logging.DEBUG)
log = logging.getLogger('flickrfs')
logattr = logging.getLogger('flickrfs.attr')
logattr.setLevel(logging.INFO)

cp = ConfigParser.ConfigParser()
cp.read(flickrfsHome + '/config.txt')
_resizeStr = ""
sets_sync_int = 600.0
stream_sync_int = 600.0
try:
  _resizeStr = cp.get('configuration', 'image.size')
except:
  print 'No default size of image found. Will upload original size of images.'
try:
  sets_sync_int = float(cp.get('configuration', 'sets.sync.int'))
except:
  pass
try:
  stream_sync_int = float(cp.get('configuration', 'stream.sync.int'))
except:
  pass
try:
  browserName = cp.get('configuration', 'browser')
except:
  pass

# Retrive the resize string.
def GetResizeStr():
  return _resizeStr

#Utility functions.
def _log_exception_wrapper(func, *args, **kw):
  """Call 'func' with args and kws and log any exception it throws.
  """
  for i in range(0, NUMRETRIES):
    log.debug("retry attempt %s for func %s", i, func.__name__)
    try:
      func(*args, **kw)
      return
    except:
      log.error("exception in function %s", func.__name__)
      log.error(format_exc())

def background(func, *args, **kw):
    """Run 'func' as a thread, logging any exceptions it throws.

    To run

      somefunc(arg1, arg2='value')

    as a thread, do:

      background(somefunc, arg1, arg2='value')

    Any exceptions thrown are logged as errors, and the traceback is logged.
    """
    thread.start_new_thread(_log_exception_wrapper, (func,)+args, kw)

def timerThread(func, func1, interval):
  '''Execute func now, followed by func1 every interval seconds
  '''
  log.debug("running first pass funtion %s", func.__name__)
  t = threading.Timer(0.0, func)
  try:
    t.run()
  except: 
    log.debug(format_exc())
  while(interval):
    log.debug("scheduling function %s to run in %s seconds",
        func1.__name__, interval)
    t = threading.Timer(interval, func1)
    try:
      t.run()
    except:
      log.debug(format_exc())

def retryFlickrOp(isNone, func, *args):
  # This function helps in retrying the flickr transactions, in case they fail.
  result = None
  for i in range(0, NUMRETRIES):
    log.debug("retry attempt %d for func %s", i, func.__name__)
    try:
      result = func(*args)
      if result is None:
        if isNone:
          return result
        else:
          continue
      else:
        return result
    except URLError, detail:
      log.error("URLError in function %s with error: %s",
          func.__name__, detail)
  # We've utilized all our attempts, send out the result whatever it is.
  return result

class Flickrfs(Fuse):

  def __init__(self, *args, **kw):
  
    Fuse.__init__(self, *args, **kw)
    log.info("mountpoint: %s", repr(self.mountpoint))
    log.info("mount options: %s", ', '.join(self.optlist))
    log.info("named mount options: %s",
        ', '.join([ "%s: %s" % (k, v) for k, v in self.optdict.items() ]))
    
    self.inodeCache = inodes.InodeCache(dbPath) # Inodes need to be stored.
    self.imgCache = inodes.ImageCache()
    self.NSID = ""
    self.transfl = TransFlickr(browserName)

    # Set some variables to be utilized by statfs function.
    self.statfsCounter = -1
    self.max = 0L
    self.used = 0L

    self.NSID = self.transfl.getUserId()
    if self.NSID is None:
      log.error("can't retrieve user information")
      sys.exit(-1)

    log.info('getting list of licenses available')
    self.licenses = self.transfl.getLicenses()
    if self.licenses is None:
      log.error("can't retrieve license information")
      sys.exit(-1)

    # do stuff to set up your filesystem here, if you want
    self._mkdir("/")
    self._mkdir("/tags")
    self._mkdir("/tags/personal")
    self._mkdir("/tags/public")
    background(timerThread, self.sets_thread, 
               self.sync_sets_thread, sets_sync_int) #sync every 2 minutes


  def imageResize(self, bufData):
    # If no resizing information is present, then return the buffer directly.
    if GetResizeStr() == "":
      return bufData

    # Else go ahead and do the conversion.
    im = '/tmp/flickrfs-' + str(int(random.random()*1000000000))
    f = open(im, 'w')
    f.write(bufData)
    f.close()
    cmd = 'identify -format "%%w" %s'%(im,)
    status,ret = commands.getstatusoutput(cmd)
    msg = ("%s command not found; you must install Imagemagick to get "
      "auto photo resizing")
    if status!=0:
      print msg % 'identify'
      log.error(msg, identify)
      return bufData
    try:
      if int(ret)<int(GetResizeStr().split('x')[0]):
        log.info('image size is smaller than size specified in config.txt;'
                 ' retaining original size')
        return bufData
    except:
      log.error('invalid format of image.size in config.txt')
      return bufData
    log.debug("resizing image %s to size %s" % (im, GetResizeStr()))
    cmd = 'convert %s -resize %s %s-conv'%(im, GetResizeStr(), im)
    ret = os.system(cmd)
    if ret!=0:
      print msg % 'convert'
      log.error(msg, 'convert')
      return bufData
    else:
      f = open(im + '-conv')
      return f.read()


  def writeMetaInfo(self, id, INFO):
    #The metadata may be unicode strings, so we need to encode them on write
    filePath = os.path.join(flickrfsHome, '.'+id)
    f = codecs.open(filePath, 'w', 'utf8')
    f.write('# Metadata file : flickrfs - Virtual filesystem for flickr\n')
    f.write('# Photo owner: %s NSID: %s\n' % (INFO[7], INFO[8]))
    f.write('# Handy link to photo: %s\n'%(INFO[9]))
    f.write('# Licences available: \n')
    for (k, v) in self.licenses:
      f.write('# %s : %s\n' % (k, v))
    f.write('[metainfo]\n')
    f.write("%s:%s\n"%('title', INFO[4]))
    f.write("%s:%s\n"%('description', INFO[3]))
    tags = ','.join(INFO[5])
    f.write("%s:%s\n"%('tags', tags))
    f.write("%s:%s\n"%('license',INFO[6]))
    f.close()
    f = open(filePath)
    f.read()
    fileSize = f.tell()
    f.close()
    return fileSize

  def __populate_set(self, set_id, curdir):
    # Exception handling will be done by background function.
    photosInSet = self.transfl.getPhotosFromPhotoset(set_id)
    for b,p in photosInSet.iteritems():
      info = self.transfl.parseInfoFromPhoto(b,p)
      self._mkfileWithMeta(curdir, info)
    log.info("set %s populated, photo count %s", curdir, len(photosInSet))

  def sets_thread(self):
    """
      The beauty of the FUSE python implementation is that with the 
      python interpreter running in foreground, you can have threads
    """
    print "Sets are being populated in the background."
    log.info("started")
    self._mkdir("/sets")
    for a in self.transfl.getPhotosetList():
      title = a.title[0].elementText.replace('/', '_')
      log.info("populating set %s", title)
      curdir = "/sets/" + title
      if title.strip()=='':
        curdir = "/sets/" + a['id']
      set_id = a['id']
      self._mkdir(curdir, id=set_id)
      self.__populate_set(set_id, curdir)

  def _sync_code(self, psetOnline, curdir):
    psetLocal = set(map(lambda x: x[0], self.getdir(curdir, False)))
    for b in psetOnline:
      info = self.transfl.parseInfoFromPhoto(b)
      imageTitle = info.get('title','')
      if hasattr(b, 'originalformat'):
        imageTitle = self.__getImageTitle(imageTitle, 
                                        b['id'], b['originalformat'])
      else:
        imageTitle = self.__getImageTitle(imageTitle, b['id'])
      path = "%s/%s"%(curdir, imageTitle)
      inode = self.inodeCache.get(path)
      # This exception throwing is just for debugging.
      if inode == None and self.inodeCache.has_key(path):
        e = OSError("Path %s present in inodeCache" % path)
        e.errno = ENOENT
        raise e
      if inode == None: # Image inode not present in the set.
        log.debug("new image found: %s", path)
        self._mkfileWithMeta(curdir, info)
      else:
        if inode.mtime != int(info.get('dupdate')):
          log.debug("image %s changed", path)
          self.inodeCache.pop(path)
          if self.inodeCache.has_key(path + ".meta"):
            self.inodeCache.pop(path + ".meta")
          self._mkfileWithMeta(curdir, info)
        psetLocal.discard(imageTitle)
    if len(psetLocal)>0:
      log.info('%s photos have been deleted online' % len(psetLocal))
    for c in psetLocal:
      log.info('deleting %s', c)
      self.unlink("%s/%s" % (curdir, c), False)

  def __sync_set_in_background(self, set_id, curdir):
    # Exception handling will be done by background function.
    log.info("syncing set %s", curdir)
    psetOnline = self.transfl.getPhotosFromPhotoset(set_id)
    self._sync_code(psetOnline, curdir)
    log.info("set %s sync successfully finished", curdir)
    
  def sync_sets_thread(self):
    log.info("started")
    setListOnline = self.transfl.getPhotosetList()
    setListLocal = self.getdir('/sets', False)
    
    for a in setListOnline:
      title = a.title[0].elementText.replace('/', '_')
      if title.strip()=="":
        title = a['id']
      if (title,0) not in setListLocal: #New set added online
        log.info("new set %s found online", title)
        self._mkdir('/sets/'+title, a['id'])
      else: #Present Online
        setListLocal.remove((title,0))
    for a in setListLocal: #List of sets present locally, but not online
      log.info('set %s no longer online, recursively deleting it', a)
      self.rmdir('/sets/'+a[0], online=False, recr=True)
        
    for a in setListOnline:
      title = a.title[0].elementText.replace('/', '_')
      curdir = "/sets/" + title
      if title.strip()=='':
        curdir = "/sets/" + a['id']
      set_id = a['id']
      self.__sync_set_in_background(set_id, curdir)
    log.info('finished')

  def sync_stream_thread(self):
    log.info('started')
    psetOnline = self.transfl.getPhotoStream(self.NSID)
    self._sync_code(psetOnline, '/stream')
    log.info('finished')
      
  def stream_thread(self):
    log.info("started")
    print "Populating photostream"
    for b in self.transfl.getPhotoStream(self.NSID):
      info = self.transfl.parseInfoFromPhoto(b)
      self._mkfileWithMeta('/stream', info)
    log.info("finished")
    print "Photostream population finished."
      
  def tags_thread(self, path):
    ind = string.rindex(path, '/')
    tagName = path[ind+1:]
    if tagName.strip()=='':
      log.error("the tagName '%s' doesn't contain any tags", tagName)
      return 
    log.info("started for %s", tagName)
    sendtagList = ','.join(tagName.split(':'))
    if(path.startswith('/tags/personal')):
      user_id = self.NSID
    else:
      user_id = None
    for b in self.transfl.getTaggedPhotos(sendtagList, user_id):
      info = self.transfl.parseInfoFromPhoto(b)
      self._mkfileWithMeta(path, info)

  def getUnixPerms(self, info):
    mode = info.get('mode')
    if mode is not None:
      return mode
    perms = info.get('perms')
    if perms is None:
      return 0644
    if perms is "1": # public
      return 0755
    elif perms is "2": # friends only. Add 1 to 4 in middle letter.
      return 0754
    elif perms is "3": # family only. Add 2 to 4 in middle letter.
      return 0764
    elif perms is "4": # friends and family. Add 1+2 to 4 in middle letter.
      return 0774
    else:
      return 0744 # private

  def __getImageTitle(self, title, id, format = "jpg"):
    temp = title.replace('/', '')
#    return "%s_%s.%s" % (temp[:32], id, format)
    # Store the photos original name. Thus, when pictures are uploaded
    # their names would remain as it is, allowing easy resumption of
    # uploading of images, in case some of the photos fail uploading.
    return "%s.%s" % (temp, format)

  def _mkfileWithMeta(self, path, info):
    # Don't write the meta information here, because it requires access to
    # the full INFO. Only do with the smaller version of information that
    # is provided.
    if info is None:
      return
    title = info.get("title", "")
    id =    info.get("id", "")
    ext =   info.get("format", "jpg")
    title = self.__getImageTitle(title, id, ext)

    # Refactor this section of code, so that it can be called
    # from read.
    # Figure out a way to retrieve information, which can be 
    # used in _mkfile.
    mtime = info.get("dupdate")
    ctime = info.get("dupload")
    perms = self.getUnixPerms(info)
    self._mkfile(path+"/"+title, id=id, mode=perms, mtime=mtime, ctime=ctime)
    self._mkfile(path+'/.'+title+'.meta', id)

  def _parsepathid(self, path, id=""):
    #Path and Id may be unicode strings, so encode them to utf8 now before
    #we use them, otherwise python will throw errors when we combine them
    #with regular strings.
    path = path.encode('utf8')
    if id!=0: id = id.encode('utf8')
    parentDir, name = os.path.split(path)
    if parentDir=='':
      parentDir = '/'
    log.debug("parentDir %s", parentDir)
    return path, id, parentDir, name

  def _mkdir(self, path, id="", mtime=None, ctime=None):
    path, id, parentDir, name = self._parsepathid(path, id)
    log.debug("creating directory %s", path)
    self.inodeCache[path] = inodes.DirInode(path, id, mtime=mtime, ctime=ctime)
    if path!='/':
      pinode = self.getInode(parentDir)
      pinode.nlink += 1
      self.updateInode(parentDir, pinode)
      log.debug("nlink of %s is now %s", parentDir, pinode.nlink)

  def _mkfile(self, path, id="", mode=None, 
              comm_meta="", mtime=None, ctime=None):
    path, id, parentDir, name = self._parsepathid(path, id)
    log.debug("creating file %s with id %s", path, id)
    image_name, extension = os.path.splitext(name)
    if not extension:
      log.error("can't create file without extension")
      return
    fInode = inodes.FileInode(path, id, mode=mode, comm_meta=comm_meta,
                              mtime=mtime, ctime=ctime)
    self.inodeCache[path] = fInode
    # Now create the meta info inode if the meta info file exists
    # refactoring: create the meta info inode, regardless of the
    # existence of datapath.
#    path = os.path.join(parentDir, '.' + image_name + '.meta')
#    datapath = os.path.join(flickrfsHome, '.'+id)
#    if os.path.exists(datapath):
#    size = os.path.getsize(datapath)
#    self.inodeCache[path] = FileInode(path, id)

  def getattr(self, path):
    # getattr is being called 4-6 times every second for '/'
    # Don't log those calls, as they clutter up the log file.
    if path != "/":
      logattr.debug("getattr: %s", path)
    templist = path.split('/')
    if path.startswith('/sets/'):
      templist[2] = templist[2].split(':')[0]
    elif path.startswith('/stream'):
      templist[1] = templist[1].split(':')[0]
    path = '/'.join(templist)

    inode=self.getInode(path)
    if inode:
      #log.debug("inode %s", inode)
      statTuple = (inode.mode,inode.ino,inode.dev,inode.nlink,
          inode.uid,inode.gid,inode.size,inode.atime,inode.mtime,inode.ctime)
      #log.debug("statsTuple %s", statTuple)
      return statTuple
    else:
      e = OSError("No such file"+path)
      e.errno = ENOENT
      raise e

  def readlink(self, path):
    log.debug("readlink")
    return os.readlink(path)
  
  def getdir(self, path, hidden=True):
    logattr.debug("getdir: %s", path)
    templist = []
    if hidden:
      templist = ['.', '..']
    for a in self.inodeCache.keys():
      ind = a.rindex('/')
      if path=='/':
        path=""
      if path==a[:ind]:
        name = a.split('/')[-1]
        if name=="":
          continue
        if hidden and name.startswith('.'):
          templist.append(name)
        elif not name.startswith('.'):
          templist.append(name)
    return map(lambda x: (x,0), templist)

  def unlink(self, path, online=True):
    log.debug("unlink %s", path)
    if self.inodeCache.has_key(path):
      inode = self.inodeCache.pop(path)
      # Remove the meta data file as well if it exists
      if self.inodeCache.has_key(path + ".meta"):
        self.inodeCache.pop(path + ".meta")

      typesinfo = mimetypes.guess_type(path)
      if typesinfo[0] is None or typesinfo[0].count('image')<=0:
        log.debug("unlinked non-image file %s", path)
        return

      if path.startswith('/sets/'):
        ind = path.rindex('/')
        pPath = path[:ind]
        pinode = self.getInode(pPath)
        if online:
          self.transfl.removePhotofromSet(photoId=inode.photoId, 
                                          photosetId=pinode.setId)
          log.info("photo %s removed from set", path)
      del inode
    else:
      log.error("%s is not a known file", path)
      #Dont' raise an exception. Not useful when
      #using editors like Vim. They make loads of 
      #crap buffer files
  
  def rmdir(self, path, online=True, recr=False):
    log.debug("removing %s", path)
    if self.inodeCache.has_key(path):
      for a in self.inodeCache.keys():
        if a.startswith(path+'/'):
          if recr:
            self.unlink(a, online)
          else:
            e = OSError("Directory not empty")
            e.errno = ENOTEMPTY
            raise e
    else:
      log.error("%s is not a known directory", path)
      e = OSError("No such folder"+path)
      e.errno = ENOENT
      raise e
      
    if path=='/sets' or path=='/tags' or path=='/tags/personal' \
        or path=='/tags/public' or path=='/stream':
      log.debug("attempt to remove framework file %s rejected", path)
      e = OSError("removal of folder %s not allowed" % (path))
      e.errno = EPERM
      raise e

    ind = path.rindex('/')
    pPath = path[:ind]
    inode = self.inodeCache.pop(path)
    if online and path.startswith('/sets/'):
      self.transfl.deleteSet(inode.setId)
    del inode
    pInode = self.getInode(pPath)
    pInode.nlink -= 1
    self.updateInode(pPath, pInode)
  
  def symlink(self, path, path1):
    log.debug("symlink")
    return os.symlink(path, path1)

  def rename(self, path, path1):
    log.debug("%s %s", path, path1)
    #Donot allow Vim to create a file~
    #Check for .meta in both paths
    if path.count('~')>0 or path1.count('~')>0:
      log.debug("vim enablement path entered")
      try:
        #Get inode, but _dont_ remove from cache
        inode = self.getInode(path)
        if inode is not None:
          self.inodeCache[path1] = inode
      except:
        log.debug("couldn't find inode for %s", path)
      return

    #Read from path
    inode = self.getInode(path)
    if inode is None or not hasattr(inode, 'photoId'):
      return
    fname = os.path.join(flickrfsHome, '.'+inode.photoId)
    f = open(fname, 'r')
    buf = f.read()
    f.close()

    #Now write to path1
    inode = self.getInode(path1)
    if inode is None or not hasattr(inode, 'photoId'):
      return
    fname = os.path.join(flickrfsHome, '.'+inode.photoId)
    f = open(fname, 'w')
    f.write(buf)
    f.close()
    inode.size = os.path.getsize(fname)
    self.updateInode(path1, inode)
    retinfo = self.parse(fname, inode.photoId)
    if retinfo.count('Error')>0:
      log.error(retinfo)
    
  def link(self, srcpath, destpath):
    log.debug("%s %s", srcpath, destpath)
    #Add image from stream to set, w/o retrieving
    slist = srcpath.split('/')
    sname_file = slist.pop(-1)
    dlist = destpath.split('/')
    dname_file = dlist.pop(-1)
    error = 0
    if sname_file=="" or sname_file.startswith('.'):
      error = 1
    if dname_file=="" or dname_file.startswith('.'):
      error = 1
    if not destpath.startswith('/sets/'):
      error = 1
    if error is 1:
      log.error("linking is allowed only between 2 image files")
      return
    sinode = self.getInode(srcpath)
    self._mkfile(destpath, id=sinode.id, mode=sinode.mode, 
                 comm_meta=sinode.comm_meta, mtime=sinode.mtime, 
                 ctime=sinode.ctime)
    parentPath = '/'.join(dlist)
    pinode = self.getInode(parentPath)
    if pinode.setId==0:
      try:
        pinode.setId = self.transfl.createSet(parentPath, sinode.photoId)
        self.updateInode(parentPath, pinode)
      except:
        e = OSError("Can't create a new set")
        e.errno = EIO
        raise e
    else:
      self.transfl.put2Set(pinode.setId, sinode.photoId)

  
  def chmod(self, path, mode):
    log.debug("%s %s" % path, mode)
    inode = self.getInode(path)
    typesinfo = mimetypes.guess_type(path)

    if inode.comm_meta is None:
      log.debug("chmod on directory ignored")
      return
        
    elif typesinfo[0] is None or typesinfo[0].count('image')<=0:
      
      os.chmod(path, mode)
      return

    elif self.transfl.setPerm(inode.photoId, mode, inode.comm_meta)==True:
      inode.mode = mode
      self.updateInode(path, inode)
      return
    
  def chown(self, path, user, group):
    log.debug("%s:%s %s (ignored)", user, group, path)
    
  def truncate(self, path, size):
    log.debug("%s %s", path, size)
    ind = path.rindex('/')
    name_file = path[ind+1:]

    typeinfo = mimetypes.guess_type(path)
    if typeinfo[0] is None or typeinfo[0].count('image')<=0:
      inode = self.getInode(path)
      filePath = os.path.join(flickrfsHome, '.'+inode.photoId)
      f = open(filePath, 'w+')
      return f.truncate(size)
    
  def mknod(self, path, mode, dev):
    log.debug("%s %s %s ", path, mode, dev)
    templist = path.split('/')
    name_file = templist[-1]

    if name_file.startswith('.') and name_file.count('.meta') > 0:
      # We need to handle the special case, where some meta files are being
      # created through mknod. Creation of meta files is done when adding
      # images automatically; and they should not go through mknod system call.
      # Editors like Vim, try to generate random swap files when reading
      # meta; and this should be *disallowed*.
      log.debug("mknod for meta file %s ignored", path)
      return

    if path.startswith('/sets/'):
      templist[2] = templist[2].split(':')[0]
    elif path.startswith('/stream'):
      templist[1] = templist[1].split(':')[0]
    path = '/'.join(templist)

    log.debug("modified file path %s", path)
    #Lets guess what kind of a file is this. 
    #Is it an image file? or, some other temporary file
    #created by the tools you're using. 
    typeinfo = mimetypes.guess_type(path)
    if typeinfo[0] is None or typeinfo[0].count('image') <= 0:
      f = open(os.path.join(flickrfsHome,'.'+name_file), 'w')
      f.close()
      # TODO(manishrjain): This should not be FileInode, it should rather be
      # Inode.
      self.inodeCache[path] = inodes.FileInode(path, name_file, mode=mode)
    else:
      self._mkfile(path, id="NEW", mode=mode)

  def mkdir(self, path, mode):
    log.debug("%s with mode %s", path, mode)
    if path.startswith("/tags"):
      if path.count('/')==3:   #/tags/personal (or private)/dirname ONLY
        self._mkdir(path)
        background(self.tags_thread, path)
      else:
        e = OSError("Not allowed to create directory %s" % path)
        e.errno = EACCES
        raise e
    elif path.startswith("/sets"):
      if path.count('/')==2:  #Only allow creation of new set /sets/newset
        self._mkdir(path, id=0)
          #id=0 means that not yet created online
      else:
        e = OSError("Not allowed to create directory %s" % path)
        e.errno = EACCES
        raise e
    elif path=='/stream':
      self._mkdir(path)
      background(timerThread, self.stream_thread, 
                 self.sync_stream_thread, stream_sync_int)
      
    else:
      e = OSError("Not allowed to create directory %s" % path)
      e.errno = EACCES
      raise e
      
  def utime(self, path, times):
    inode = self.getInode(path)
    inode.atime = times[0]
    inode.mtime = times[1]
    self.updateInode(path, inode)
    return 0

  def open(self, path, flags):
    log.info("%s with flags %s", path, flags)
    ind = path.rindex('/')
    name_file = path[ind+1:]
    if name_file.startswith('.') and name_file.endswith('.meta'):
      self.handleAccessToNonImage(path)
      return 0
    typesinfo = mimetypes.guess_type(path)
    if typesinfo[0] is None or typesinfo[0].count('image')<=0:
      log.debug('non-image file found %s', path)
      self.handleAccessToNonImage(path)
      return 0
    
    templist = path.split('/')
    if path.startswith('/sets/'):
      templist[2] = templist[2].split(':')[0]
    elif path.startswith('/stream'):
      templist[1] = templist[1].split(':')[0]
    path = '/'.join(templist)
    log.debug("path after modification is %s", path)
    
    inode = self.getInode(path)
    if inode.photoId=="NEW": #Just skip if new (i.e. uploading)
      return 0
    if self.imgCache.getBuffer(inode.photoId)=="":  
      log.debug("retrieving image %s from flickr", inode.photoId)
      self.imgCache.setBuffer(inode.photoId,
          str(self.transfl.getPhoto(inode.photoId)))
      inode.size = self.imgCache.getBufLen(inode.photoId)
      log.debug("size of image is %s", inode.size)
      self.updateInode(path, inode)
    return 0
    
  def read(self, path, length, offset):
    log.debug("%s offset %s length %s", path, offset, length)
    ind = path.rindex('/')
    name_file = path[ind+1:]
    if name_file.startswith('.') and name_file.endswith('.meta'):
      # Check if file is not present. If not, retrieve and 
      # create the file locally.
      buf = self.handleReadNonImage(path, length, offset)
      return buf
    typesinfo = mimetypes.guess_type(path)
    if typesinfo[0] is None or typesinfo[0].count('image')<=0:
      return self.handleReadNonImage(path, length, offset)
    return self.handleReadImage(path, length, offset)

  def parse(self, fname, photoId):
    cp = ConfigParser.ConfigParser()
    log.debug("parsing file %s for photoid %s", fname, photoId)
    cp.read(fname)
    log.debug("file %s has been read by ConfigParser", fname)
    options = cp.options('metainfo')
    title=''
    desc=''
    tags=''
    license=''
    if 'description' in options:
      desc = cp.get('metainfo', 'description')
    if 'tags' in options:
      tags = cp.get('metainfo', 'tags')
    if 'title' in options:
      title = cp.get('metainfo', 'title')
    if 'license' in options:
      license = cp.get('metainfo', 'license')
      
    log.debug("setting metadata for file %s", fname)
    if self.transfl.setMeta(photoId, title, desc)==False:
      return "Error:Can't set Meta information"
      
    log.debug("setting tags for %s", fname)
    if self.transfl.setTags(photoId, tags)==False:
      log.debug("setting tags for %s failed", fname)
      return "Error:Can't set tags"

    log.debug("setting license for %s", fname)
    if self.transfl.setLicense(photoId, license)==False:
      return "Error:Can't set license"
            
 #   except:
 #     log.error("Can't parse file:%s:"%(fname,))
 #     return "Error:Can't parse"
    return 'Success:Updated photo:%s:%s:'%(fname,photoId)

  ##################################################
  # 'handle' Functions for handling read and writes.
  ##################################################
  def handleAccessToNonImage(self, path):
    inode = self.getInode(path)
    if inode is None:
      log.error("inode %s doesn't exist", path)
      e = OSError("No inode found")
      e.errno = EIO
      raise e
    fname = os.path.join(flickrfsHome, '.'+inode.photoId) #ext
    # Handle the case when file already exists.
    if not os.path.exists(fname) or os.path.getsize(fname) == 0L:
      log.info("retrieving meta information for file %s and photo id %s", 
               fname, inode.photoId)
      INFO = self.transfl.getPhotoInfo(inode.photoId)
      size = self.writeMetaInfo(inode.photoId, INFO)
      log.info("information has been written for photo id %s", inode.photoId)
      inode.size = size
      self.updateInode(path, inode)
      time.sleep(1) # Enough time for OS to call for getattr again.
    return inode

  def handleReadNonImage(self, path, length, offset):
    inode = self.handleAccessToNonImage(path)
    f = open(os.path.join(flickrfsHome, '.'+inode.photoId), 'r')
    f.seek(offset)
    return f.read(length)
  
  def handleReadImage(self, path, length, offset):
    inode = self.getInode(path)
    if inode is None:
      log.error("inode %s doesn't exist", path)
      e = OSError("No inode found")
      e.errno = EIO
      raise e
    if self.imgCache.getBufLen(inode.photoId) is 0:  
      log.debug("retrieving image %s from flickr", inode.photoId)
      buf = retryFlickrOp(False, self.transfl.getPhoto,
                          inode.photoId)
      if len(buf) == 0:
        log.error("can't retrieve image %s", inode.photoId)
        e = OSError("Unable to retrieve image.")
        e.errno = EIO
        raise e
      self.imgCache.setBuffer(inode.photoId, buf)
      inode.size = self.imgCache.getBufLen(inode.photoId)
    temp =  self.imgCache.getBuffer(inode.photoId, offset, offset+length)
    if len(temp) < length:
      self.imgCache.popBuffer(inode.photoId)
    self.updateInode(path, inode)
    return temp

  def handleWriteToNonImage(self, path, buf, off):
    inode = self.handleAccessToNonImage(path)
    fname = os.path.join(flickrfsHome, '.'+inode.photoId) #ext
    log.debug("writing to %s", fname)
    f = open(fname, 'r+')
    f.seek(off)
    f.write(buf)
    f.close()
    if len(buf)<4096:
      inode.size = os.path.getsize(fname)
      retinfo = self.parse(fname, inode.photoId)
      if retinfo.count('Error')>0:
        e = OSError(retinfo.split(':')[1])
        e.errno = EIO
        raise e
      self.updateInode(path, inode)
    return len(buf)

  def handleUploadingImage(self, path, inode, taglist):
    tags = [ '"%s"'%(a,) for a in taglist]
    tags.append('flickrfs')
    taglist = ' '.join(tags)
    log.info('uploading %s with len %s', 
             path, self.imgCache.getBufLen(inode.photoId))
    id = None
    bufData = self.imgCache.getBuffer(inode.photoId)
    bufData = self.imageResize(bufData)
    id = retryFlickrOp(False, self.transfl.uploadfile,
                       path, taglist, bufData, inode.mode)
    if id is None:
      log.error("unable to upload file %s", inode.photoId)
      e = OSError("Unable to upload file.")
      e.errno = EIO
      raise e
    self.imgCache.popBuffer(inode.photoId)
    inode.photoId = id
    self.updateInode(path, inode)
    return inode

  def handleWriteToBuffer(self, path, buf):
    inode = self.getInode(path)
    if inode is None:
      log.error("inode %s doesn't exist", path)
      e = OSError("No inode found")
      e.errno = EIO
      raise e
    self.imgCache.addBuffer(inode.photoId, buf)
    return inode

  def handleWriteAddToSet(self, parentPath, pinode, inode):
    #Create set if it doesn't exist online (i.e. if id=0)
    if pinode.setId is 0:
      # Retry creation of set if unsuccessful.
      pinode.setId = retryFlickrOp(False, self.transfl.createSet,
                                   parentPath, inode.photoId)
      # If the set is created, then return.
      if pinode.setId is not None:
        self.updateInode(parentPath, pinode)
        return
      else:
        log.error("unable to create set %s", parentPath)
        e = OSError("Unable to create set.")
        e.errno = EIO
        raise e
    else:
      # If the operation put2Set doesn't throw exception, that means
      # that the picture has been successfully added to set.
      # Return in that case, retry otherwise.
      retryFlickrOp(True, self.transfl.put2Set,
                    pinode.setId, inode.photoId)
      return

  #############################
  # End of 'handle' Functions.
  #############################

  def write(self, path, buf, off):
    log.debug("write to %s at offset %s", path, off)
    ind = path.rindex('/')
    name_file = path[ind+1:]
    if name_file.startswith('.') and name_file.count('.meta')>0:
      return self.handleWriteToNonImage(path, buf, off)
    typesinfo = mimetypes.guess_type(path)
    if typesinfo[0] is None or typesinfo[0].count('image')<=0:
      return self.handleWriteToNonImage(path, buf, off)
    templist = path.split('/')
    inode = None
    if path.startswith('/tags'):
      e = OSError("Copying to tags not allowed")
      e.errno = EIO
      raise e
    if path.startswith('/stream'):
      tags = templist[1].split(':')
      templist[1] = tags.pop(0)
      path = '/'.join(templist)
      inode = self.handleWriteToBuffer(path, buf)
      if len(buf) < 4096:
        self.handleUploadingImage(path, inode, tags)
    elif path.startswith('/sets/'):
      setnTags = templist[2].split(':')
      setName = setnTags.pop(0)
      templist[2] = setName
      path = '/'.join(templist)
      inode = self.handleWriteToBuffer(path, buf)
      if len(buf) < 4096:
        templist.pop(-1)
        parentPath = '/'.join(templist)
        pinode = self.getInode(parentPath)
        inode = self.handleUploadingImage(path, inode, setnTags)
        self.handleWriteAddToSet(parentPath, pinode, inode)
    log.debug("done write to %s at offset %s", path, off)
    if len(buf)<4096:
      templist = path.split('/')
      templist.pop(-1)
      parentPath = '/'.join(templist)
      try:
        self.inodeCache.pop(path)
      except:
        pass
      INFO = self.transfl.getPhotoInfo(inode.photoId)
      info = self.transfl.parseInfoFromFullInfo(inode.photoId, INFO)
      self._mkfileWithMeta(parentPath, info)
      self.writeMetaInfo(inode.photoId, INFO)
    return len(buf)

  def getInode(self, path):
    if self.inodeCache.has_key(path):
      #log.debug("got cached inode for %s", path)
      return self.inodeCache[path]
    else:
      #log.debug("%s is not in inode cache", path)
      return None

  def updateInode(self, path, inode):
    self.inodeCache[path] = inode

  def release(self, path, flags):
    log.debug("%s with flags %s ignored", path, flags)
    return 0
  
  def statfs(self):
    """
  Should return a tuple with the following elements in respective order:
  
  F_BSIZE - Preferred file system block size. (int)
  F_FRSIZE - Fundamental file system block size. (int)
  F_BLOCKS - Total number of blocks in the filesystem. (long)
  F_BFREE - Total number of free blocks. (long)
  F_BAVAIL - Free blocks available to non-super user. (long)
  F_FILES - Total number of file nodes. (long)
  F_FFREE - Total number of free file nodes. (long)
  F_FAVAIL - Free nodes available to non-super user. (long)
  F_FLAG - Flags. System dependent: see statvfs() man page. (int)
  F_NAMEMAX - Maximum file name length. (int)
  Feel free to set any of the above values to 0, which tells
  the kernel that the info is not available.
    """
    block_size = 1024
    blocks = 0L
    blocks_free = 0L
    files = 0L
    files_free = 0L
    namelen = 255
    # statfs is being called repeatedly at least once a second.
    # The bandwidth information doesn't change that often, so
    # save upon communication with flickr servers to retrieve this
    # information. Only retrieve it once in a while.
    if self.statfsCounter >= 500 or self.statfsCounter is -1:
      (self.max, self.used) = self.transfl.getBandwidthInfo()
      self.statfsCounter = 0
      log.info('retrieved bandwidth info: max %s used %s', 
               self.max, self.used)
    self.statfsCounter = self.statfsCounter + 1

    if self.max is not None:
      blocks = long(self.max)/block_size
      blocks_used = long(self.used)/block_size
      blocks_free = blocks - blocks_used
      blocks_available = blocks_free
      return (block_size, blocks, blocks_free, blocks_available, 
              files, files_free, namelen)

  def fsync(self, path, isfsyncfile):
    log.debug("%s,  isfsyncfile=%s", path, isfsyncfile)
    return 0


if __name__ == '__main__':
  try:
    server = Flickrfs()
    server.multithreaded = 1;
    server.main()
  except KeyError:
    log.error('got key error; exiting...')
    sys.exit(0)
