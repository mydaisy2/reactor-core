#!/usr/bin/make -f

VERSION := $(shell date "+%Y%m%d.%H%M%S")

all : clean
	@VERSION=$(VERSION) python setup.py install --prefix=$$PWD/dist/usr --root=/
	@cd $$PWD/dist/usr/lib/python* && [ -d site-packages ] && \
	    mv site-packages dist-packages || true
	@mkdir -p $$PWD/dist/etc/init
	@install -m0644 etc/pancake.conf $$PWD/dist/etc/init
	@cd $$PWD/dist && tar cvzf ../pancake-$(VERSION).tgz .
.PHONY: all

clean :
	@make -C image clean
	@rm -rf dist build pancake.* pancake-*.tgz
.PHONY: clean

# Install the latest package (for python bindings).
contrib/zookeeper-3.4.3 : contrib/zookeeper-3.4.3.tar.gz
	@cd contrib; tar xzf zookeeper-3.4.3.tar.gz
	@cd contrib/zookeeper-3.4.3/src/c; autoreconf -if && ./configure

# Build the appropriate python bindings.
image/contrib/python-zookeeper-3.4.3.tgz : contrib/zookeeper-3.4.3
	@mkdir -p dist-zookeeper
	@cd contrib/zookeeper-3.4.3/src/c; make install DESTDIR=$$PWD/../../../../dist-zookeeper/
	@cd contrib/zookeeper-3.4.3/src/contrib/zkpython; ant tar-bin
	@cd dist-zookeeper; tar zxf ../contrib/zookeeper-3.4.3/build/contrib/zkpython/dist/*.tar.gz
	@cd dist-zookeeper; mv usr/local/* usr; rm -rf usr/local
	@cd dist-zookeeper; fakeroot tar zcvf ../$@ .
	@rm -rf dist-zookeeper
 
# Build the development environment by installing all of the dependent packages.
# Check the README for a list of packages that will be installed.
env :
	sudo apt-get -y install nginx
	sudo apt-get -y install python-mako
	sudo apt-get -y install python-zookeeper
	sudo apt-get -y install python-novaclient
	sudo apt-get -y install python-netifaces
	sudo apt-get -y install python-pyramid || sudo easy-install pyramid 
.PHONY : env

# Build a virtual machine image for the given hypervisor.
image-% : all image/contrib/python-zookeeper-3.4.3.tgz
	@mkdir -p image/local
	@cp pancake-$(VERSION).tgz image/local
	@sudo make -C image build-$*
	@sudo chmod -R a+r image
	@GID=`id -g`; sudo find image -uid 0 -exec chown $$UID:$$GID {} \;
