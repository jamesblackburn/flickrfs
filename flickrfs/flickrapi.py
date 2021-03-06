# Flickr API implementation
#
# Inspired largely by Michele Campeotto's flickrclient and Aaron Swartz'
# xmltramp... but I wanted to get a better idea of how python worked in
# those regards, so I mostly worked those components out for myself.
#
# http://micampe.it/things/flickrclient
# http://www.aaronsw.com/2002/xmltramp/
#
# Release 1: initial release
# Release 2: added upload functionality
# Release 3: code cleanup, convert to doc strings
# Release 4: better permission support
# Release 5: converted into fuller-featured "flickrapi"
# Release 6: fix upload sig bug (thanks Deepak Jois), encode test output
# Release 7: fix path construction, Manish Rai Jain's improvements, exceptions
# Release 8: change API endpoint to "api.flickr.com"
# Release 9: change to MIT license
# Release 10: fix horrid \r\n bug on final boundary
# Release 11: break out validateFrob() for subclassing
#
# Work by (or inspired by) Manish Rai Jain <manishrjain@gmail.com>:
#
#    improved error reporting, proper multipart MIME boundary creation,
#    use of urllib2 to allow uploads through a proxy, upload accepts
#    raw data as well as a filename
#
# Copyright (c) 2007 Brian "Beej Jorgensen" Hall
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# Certain previous versions of this API were granted to the public
# domain.  You're free to use those as you please.
#
# Beej Jorgensen, Maintainer, 19-Jan-2007
# beej@beej.us
#
#------------------------------------------
# Modified(debugged portions + added functionality) by Manish Rai Jain <manishrjain@gmail.com>
# If you are interested in finding out exactly
# what portions I have modified, just search for 'manish'. Each change is 
# tagged with my name for easy locate. 
# Additional modifications similarly tagged by R. David Murray <rdmurray@bitdance.com>

import sys
import md5
import string
import urllib
import httplib
import os.path
import xml.dom.minidom
import urllib2
import socket
DEBUG = 0
########################################################################
# XML functionality
########################################################################

#-----------------------------------------------------------------------
class XMLNode:
	"""XMLNode -- generic class for holding an XML node
	xmlStr = \"\"\"<xml foo="32">
	<name bar="10">Name0</name>
	<name bar="11" baz="12">Name1</name>
	</xml>\"\"\"

	f = XMLNode.parseXML(xmlStr)

	print f.elementName
	print f['foo']
	print f.name
	print f.name[0].elementName
	print f.name[0]["bar"]
	print f.name[0].elementText
	print f.name[1].elementName
	print f.name[1]["bar"]
	print f.name[1]["baz"]

	"""

	def __init__(self):
		"""Construct an empty XML node."""
		self.elementName=""
		self.elementText=""
		self.attrib={}
		self.xml=""

	def __setitem__(self, key, item):
		"""Store a node's attribute in the attrib hash."""
		self.attrib[key] = item

	def __getitem__(self, key):
		"""Retrieve a node's attribute from the attrib hash."""
		return self.attrib[key]

	# Modified here: add a couple of methods to make it even easier to handle errors.
	# Mod by: R. David Murray <rdmurray@bitdance.com>
	def __nonzero__(self):
		if self['stat'] == "fail": return False
		return True

	def get_errortext(self):
		if self: return ''
		return "%s: error %s: %s\n" % (self.elementName, \
				self.err[0]['code'], self.err[0]['msg'])
	errormsg = property(get_errortext)

	#-----------------------------------------------------------------------
	#@classmethod
	def parseXML(cls, xmlStr="", storeXML=False):
		"""Convert an XML string into a nice instance tree of XMLNodes.

		xmlStr -- the XML to parse
		storeXML -- if True, stores the XML string in the root XMLNode.xml

		"""

		def __parseXMLElement(element, thisNode):
			"""Recursive call to process this XMLNode."""
			thisNode.elementName = element.nodeName

			#print element.nodeName

			# add element attributes as attributes to this node
			for i in range(element.attributes.length):
				an = element.attributes.item(i)
				thisNode[an.name] = an.nodeValue

			for a in element.childNodes:
				if a.nodeType == xml.dom.Node.ELEMENT_NODE:

					child = XMLNode()
					try:
						list = getattr(thisNode, a.nodeName)
					except AttributeError:
						setattr(thisNode, a.nodeName, [])

					# add the child node as an attrib to this node
					list = getattr(thisNode, a.nodeName);
					#print "appending child: %s to %s" % (a.nodeName, thisNode.elementName)
					list.append(child);

					__parseXMLElement(a, child)

				elif a.nodeType == xml.dom.Node.TEXT_NODE:
					thisNode.elementText += a.nodeValue
			
			return thisNode
		dom = xml.dom.minidom.parseString(xmlStr)

		# get the root
		rootNode = XMLNode()
		if storeXML: rootNode.xml = xmlStr

		return __parseXMLElement(dom.firstChild, rootNode)

########################################################################
# Flickr functionality
########################################################################

#-----------------------------------------------------------------------
class FlickrAPI:
	"""Encapsulated flickr functionality.

	Example usage:

	  flickr = FlickrAPI(flickrAPIKey, flickrSecret)
	  rsp = flickr.auth_checkToken(api_key=flickrAPIKey, auth_token=token)

	"""
	flickrHost = "flickr.com"
	flickrRESTForm = "/services/rest/"
	flickrAuthForm = "/services/auth/"
	flickrUploadForm = "/services/upload/"
	#-------------------------------------------------------------------
	def __init__(self, apiKey, secret):
		"""Construct a new FlickrAPI instance for a given API key and secret."""
		self.apiKey = apiKey
		self.secret = secret

		self.__handlerCache={}
		socket.setdefaulttimeout(10)
	#-------------------------------------------------------------------
	def __sign(self, data):
		"""Calculate the flickr signature for a set of params.

		data -- a hash of all the params and values to be hashed, e.g.
		        {"api_key":"AAAA", "auth_token":"TTTT"}

		"""
		dataName = ""
		if self.secret is not None:
			dataName = self.secret
		#print data
		keys = data.keys()
		keys.sort()

		for a in keys:
			if data[a] is not None:
				dataName += "%s%s" % (a, data[a])
		#print 'dataName:', dataName
		hash = md5.new()
		hash.update(dataName)
		return hash.hexdigest()

	#-------------------------------------------------------------------
	def __getattr__(self, method, **arg):
		"""Handle all the flickr API calls.
		
		This is Michele Campeotto's cleverness, wherein he writes a
		general handler for methods not defined, and assumes they are
		flickr methods.  He then converts them to a form to be passed as
		the method= parameter, and goes from there.

		http://micampe.it/things/flickrclient

		My variant is the same basic thing, except it tracks if it has
		already created a handler for a specific call or not.

		example usage:

			flickr.auth_getFrob(api_key="AAAAAA")
			rsp = flickr.favorites_getList(api_key=flickrAPIKey, \\
				auth_token=token)

		"""

		if not self.__handlerCache.has_key(method):
			def handler(_self = self, _method = method, **arg):
				_method = "flickr." + _method.replace("_", ".")
				url = "http://" + FlickrAPI.flickrHost + \
					FlickrAPI.flickrRESTForm
				arg["method"] = _method
				# Modified here: use default api_key and auth_token if not supplied
				# Mod by: R. David Murray <rdmurray@bitdance.com>
	
				#API Key is used in all the methods. So, instead of specifying it as 
				#parameter in calling function, we can append it over here by default.
				#Token eq. to Authentication is not required for ALL methods. So, 
				#better specify when needed. -Manish

				if not 'api_key' in arg: arg["api_key"] = _self.apiKey
#				if not 'auth_token' in arg and hasattr(_self, 'token'): arg['auth_token'] = _self.token

				postData = str(urllib.urlencode(arg)) + "&api_sig=" + \
					str(_self.__sign(arg))
				if DEBUG:
					print "--url---------------------------------------------"
					print url
					print "--postData----------------------------------------"
					print postData
				data = '<rsp stat="ok"></rsp>'
				req = urllib2.Request(url, postData)
#				socket.defaulttimetout(60)
				f = urllib2.urlopen(req)
				data = f.read()
				if DEBUG:
					print "--response----------------------------------------"
					print data
				f.close()
				tempNode = XMLNode()
				# Modified here: added XMLNode() as the 1st argument
				# Mod by: Manish Rai Jain <manishrjain@gmail.com>
				return XMLNode.parseXML(XMLNode(),xmlStr=data, storeXML=True)

			self.__handlerCache[method] = handler;

		return self.__handlerCache[method]
	
	#-------------------------------------------------------------------
	def __getAuthURL(self, perms, frob):
		"""Return the authorization URL to get a token.

		This is the URL the app will launch a browser toward if it
		needs a new token.
			
		perms -- "read", "write", or "delete"
		frob -- picked up from an earlier call to FlickrAPI.auth_getFrob()

		"""

		data = {"api_key": self.apiKey, "frob": frob, "perms": perms}
		data["api_sig"] = self.__sign(data)
		return "http://%s%s?%s" % (FlickrAPI.flickrHost, \
			FlickrAPI.flickrAuthForm, urllib.urlencode(data))

	#-------------------------------------------------------------------
	def upload(self, filename, jpegData="", **arg):
		"""Upload a file to flickr.

		jpegData -- send buffered data read from file instead of filename
		
		Be extra careful you spell the parameters correctly, or you will
		get a rather cryptic "Invalid Signature" error on the upload!

		Supported parameters:

		api_key
		auth_token -- documentation mistakenly calls this "auth_hash"
		title
		description
		tags -- space-delimited list of tags, "tag1 tag2 tag3"
		is_public -- "1" or "0"
		is_friend -- "1" or "0"
		is_family -- "1" or "0"

		"""

		# verify key names
		for a in arg.keys():
			if a != "api_key" and a != "auth_token" and a != "title" and \
				a != "description" and a != "tags" and a != "is_public" and \
				a != "is_friend" and a != "is_family":

				sys.stderr.write("FlickrAPI: warning: unknown parameter " \
					"\"%s\" sent to FlickrAPI.upload\n" % (a))
		
		# Modified here: use default api_key and auth_token if not supplied
		# Mod by: R. David Murray <rdmurray@bitdance.com>
		if not 'api_key' in arg: arg["api_key"] = self.apiKey
		if not 'auth_token' in arg: arg['auth_token'] = self.token
		arg["api_sig"] = self.__sign(arg)
		url = "http://" + FlickrAPI.flickrHost + FlickrAPI.flickrUploadForm

		# construct POST data
#		boundary = "===beej=jorgensen==========7d45e178b0434"
		import mimetools
		boundary = mimetools.choose_boundary()
		Hdr = "multipart/form-data; boundary=%s" % boundary
		body = ""

		# required params
		for a in ('api_key', 'auth_token', 'api_sig'):
			body += "--%s\r\n" % (boundary)
			body += "Content-Disposition: form-data; name=\""+a+"\"\r\n\r\n"
			body += "%s\r\n" % (arg[a])

		# optional params
		for a in ('title', 'description', 'tags', 'is_public', \
			'is_friend', 'is_family'):

			if arg.has_key(a):
				body += "--%s\r\n" % (boundary)
				body += "Content-Disposition: form-data; name=\""+a+"\"\r\n\r\n"
				body += "%s\r\n" % (arg[a])

		body += "--%s\r\n" % (boundary)
		body += "Content-Disposition: form-data; name=\"photo\";"
		body += " filename=\"%s\"\r\n" % filename
		body += "Content-Type: image/jpeg\r\n\r\n"

		#print body

		try:
		# Added by Manish <manishrjain@gmail.com> for allowing upload
		# by sending buffer instead of specifying filename
			if jpegData=="":
				fp = file(filename, "rb")
				jpegData = fp.read()
				fp.close()

			postData = body.encode("utf_8") + jpegData + "\r\n" + \
				("--%s--" % (boundary)).encode("utf_8")
		
		
		# Modified by Manish <manishrjain@gmail.com> for allowing
		# upload through proxy
		
			request = urllib2.Request(url)
			request.add_data(postData)
			request.add_header("Content-Type", Hdr)
			response = urllib2.urlopen(request)
			rspXML = response.read()
			# Modified by Manish Rai Jain <manishrjain@gmail.com>
			return XMLNode.parseXML(XMLNode(), xmlStr=rspXML)
		except IOError:
			return None



	#-----------------------------------------------------------------------
	#@classmethod
	def testFailure(cls, rsp, exit=True):
		"""Exit app if the rsp XMLNode indicates failure."""
		if rsp['stat'] == "fail":
			sys.stderr.write("%s: error %s: %s\n" % (rsp.elementName, \
				rsp.err[0]['code'], rsp.err[0]['msg']))
			if exit: sys.exit(1)

	#-----------------------------------------------------------------------
	def __getCachedTokenPath(self):
		"""Return the directory holding the app data."""
		return os.path.expanduser("~/.flickr/%s" % (self.apiKey))

	#-----------------------------------------------------------------------
	def __getCachedTokenFilename(self):
		"""Return the full pathname of the cached token file."""
		return "%s/auth.xml" % (self.__getCachedTokenPath())

	#-----------------------------------------------------------------------
	def __getCachedToken(self):
		"""Read and return a cached token, or None if not found.

		The token is read from the cached token file, which is basically the
		entire RSP response containing the auth element.
		"""

		try:
			f = file(self.__getCachedTokenFilename(), "r")
			data = f.read()
			f.close()
			# Modified here: added XMLNode() as the 1st argument
			# Mod by: Manish Rai Jain <manishrjain@gmail.com>
			rsp = XMLNode.parseXML(XMLNode(), data)

			return rsp.auth[0].token[0].elementText

		except IOError:
			return None

	#-----------------------------------------------------------------------
	def __setCachedToken(self, xml):
		"""Cache a token for later use.

		The cached tag is stored by simply saving the entire RSP response
		containing the auth element.

		"""

		path = self.__getCachedTokenPath()
		if not os.path.exists(path):
			os.makedirs(path)

		f = file(self.__getCachedTokenFilename(), "w")
		f.write(xml)
		f.close()


	#-----------------------------------------------------------------------
	def getToken(self, perms="write", browser="lynx"):
		"""Get a token either from the cache, or make a new one from the
		frob.

		This first attempts to find a token in the user's token cache on
		disk.
		
		If that fails (or if the token is no longer valid based on
		flickr.auth.checkToken) a new frob is acquired.  The frob is
		validated by having the user log into flickr (with lynx), and
		subsequently a valid token is retrieved.

		The newly minted token is then cached locally for the next run.

		perms--"read", "write", or "delete"
		browser--whatever browser should be used in the system() call

		"""
		
		# see if we have a saved token
		token = self.__getCachedToken()

		# see if it's valid
		if token != None:
			rsp = self.auth_checkToken(api_key=self.apiKey, auth_token=token)
			if rsp['stat'] != "ok":
				token = None
			else:
				# see if we have enough permissions
				tokenPerms = rsp.auth[0].perms[0].elementText
				if tokenPerms == "read" and perms != "read": token = None
				elif tokenPerms == "write" and perms == "delete": token = None

		# get a new token if we need one
		if token == None:
			# get the frob
			rsp = self.auth_getFrob(api_key=self.apiKey)
			self.testFailure(rsp)
			frob = rsp.frob[0].elementText

			# validate online
			os.system("%s '%s'" % (browser, self.__getAuthURL(perms, frob)))
			# get a token
			rsp = self.auth_getToken(api_key=self.apiKey, frob=frob)
			self.testFailure(rsp)
			token = rsp.auth[0].token[0].elementText
			
			# store the auth info for next time
			self.__setCachedToken(rsp.xml)

		# Modified here: save the auth token to use as a default
		# Mod by: R. David Murray <rdmurray@bitdance.com>
		self.token = token
		return token


########################################################################
# App functionality
########################################################################

def main(argv):
	# flickr auth information:
	flickrAPIKey = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # API key
	flickrSecret = "yyyyyyyyyyyyyyyy"                  # shared "secret"

	# make a new FlickrAPI instance
	fapi = FlickrAPI(flickrAPIKey, flickrSecret)

	# do the whole whatever-it-takes to get a valid token:
	token = fapi.getToken(browser="/usr/bin/x-www-browser")

	# get my favorites
	rsp = fapi.favorites_getList(api_key=flickrAPIKey,auth_token=token)
	fapi.testFailure(rsp)

	# and print them
	for a in rsp.photos[0].photo:
		print "%10s: %s" % (a['id'], a['title'].encode("ascii", "replace"))

	# upload the file foo.jpg
	#rsp = fapi.upload("foo.jpg", api_key=flickrAPIKey, auth_token=token, \
	#	title="This is the title", description="This is the description", \
	#	tags="tag1 tag2 tag3", is_public="1")
	#if rsp == None:
	#	sys.stderr.write("can't find file\n")
	#else:
	#	fapi.testFailure(rsp)

	return 0

# run the main if we're not being imported:
if __name__ == "__main__": sys.exit(main(sys.argv))

